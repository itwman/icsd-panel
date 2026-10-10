// Package link keeps one outbound WebSocket to the hub.
//
// The agent always dials out; the hub never connects to the agent. So no
// port is opened on the managed server, and a server behind NAT or a strict
// firewall works as long as it can reach the hub over HTTPS.
//
// اتصال همیشه از agent به hub است؛ روی سرورِ مدیریت‌شده هیچ پورتی باز نمی‌شود.
package link

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand/v2"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"

	"github.com/itwman/icsd-panel/agent/internal/collect"
	"github.com/itwman/icsd-panel/agent/internal/config"
)

// CloseUnauthorized is sent by the hub when the server was deleted or its
// secret revoked. Retrying fast would only hammer the hub.
const CloseUnauthorized websocket.StatusCode = 4401

const maxBuffered = 120 // ~10 minutes at 5s: brief outages leave no gap

type Envelope struct {
	T    string        `json:"t"`
	V    string        `json:"v,omitempty"`
	ID   string        `json:"id,omitempty"`
	Host *collect.Host `json:"host,omitempty"`
	D    any           `json:"d,omitempty"`
}

type inbound struct {
	T        string `json:"t"`
	ID       string `json:"id"`
	Op       string `json:"op"`
	Interval int    `json:"interval"`
}

type Agent struct {
	Cfg     *config.Config
	Col     *collect.Collector
	Version string

	mu       sync.Mutex
	queue    []collect.Sample
	wake     chan struct{}
	interval time.Duration
	reset    chan time.Duration
}

func New(cfg *config.Config, col *collect.Collector, version string) *Agent {
	return &Agent{
		Cfg: cfg, Col: col, Version: version,
		wake:     make(chan struct{}, 1),
		reset:    make(chan time.Duration, 1),
		interval: time.Duration(cfg.Interval) * time.Second,
	}
}

// Run samples forever and ships samples whenever the hub is reachable.
func (a *Agent) Run(ctx context.Context) {
	go a.sampler(ctx)
	backoff := time.Second
	for ctx.Err() == nil {
		started := time.Now()
		err := a.session(ctx)
		if ctx.Err() != nil {
			return
		}
		wait := backoff
		var ce websocket.CloseError
		switch {
		case errors.As(err, &ce) && ce.Code == CloseUnauthorized:
			log.Printf("hub rejected this agent (%s); server deleted or secret revoked — retrying in 5m", ce.Reason)
			wait = 5 * time.Minute
		case err != nil:
			log.Printf("hub link: %v — retrying in %s", err, wait)
		}
		if time.Since(started) > time.Minute {
			backoff = time.Second // it was a healthy session; start over
		} else if backoff < time.Minute {
			backoff *= 2
		}
		jitter := time.Duration(rand.Int64N(int64(wait/4) + 1))
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait + jitter):
		}
	}
}

func (a *Agent) sampler(ctx context.Context) {
	t := time.NewTicker(time.Duration(a.Cfg.Interval) * time.Second)
	defer t.Stop()
	a.Col.Prime() // the first pushed sample then has real rates
	for {
		select {
		case <-ctx.Done():
			return
		case d := <-a.reset:
			t.Reset(d)
		case <-t.C:
			a.push(a.Col.Sample())
		}
	}
}

func (a *Agent) push(s collect.Sample) {
	a.mu.Lock()
	a.queue = append(a.queue, s)
	if over := len(a.queue) - maxBuffered; over > 0 {
		a.queue = append(a.queue[:0], a.queue[over:]...)
	}
	a.mu.Unlock()
	select {
	case a.wake <- struct{}{}:
	default:
	}
}

func (a *Agent) take() []collect.Sample {
	a.mu.Lock()
	defer a.mu.Unlock()
	q := a.queue
	a.queue = nil
	return q
}

func (a *Agent) requeue(rest []collect.Sample) {
	a.mu.Lock()
	a.queue = append(rest, a.queue...)
	if over := len(a.queue) - maxBuffered; over > 0 {
		a.queue = a.queue[over:]
	}
	a.mu.Unlock()
}

func wsURL(hub string) string {
	return "ws" + strings.TrimPrefix(hub, "http") + "/api/agent/ws"
}

func (a *Agent) header() http.Header {
	h := http.Header{}
	h.Set("Authorization", "Bearer "+a.Cfg.ServerID+":"+a.Cfg.Secret)
	h.Set("X-Agent-Version", a.Version)
	return h
}

func (a *Agent) session(ctx context.Context) error {
	dctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	conn, resp, err := websocket.Dial(dctx, wsURL(a.Cfg.Hub), &websocket.DialOptions{HTTPHeader: a.header()})
	cancel()
	if err != nil {
		if resp != nil && resp.StatusCode == http.StatusUnauthorized {
			return websocket.CloseError{Code: CloseUnauthorized, Reason: "HTTP 401"}
		}
		return err
	}
	defer conn.CloseNow()
	conn.SetReadLimit(1 << 20)
	log.Printf("connected to %s", a.Cfg.Hub)

	sctx, stop := context.WithCancel(ctx)
	defer stop()
	host := a.Col.Host()
	if err := a.send(sctx, conn, Envelope{T: "hello", V: a.Version, Host: &host}); err != nil {
		return err
	}

	errc := make(chan error, 1)
	go func() { errc <- a.reader(sctx, conn) }()

	ping := time.NewTicker(30 * time.Second)
	defer ping.Stop()
	for {
		if batch := a.take(); len(batch) > 0 {
			for i, s := range batch {
				if err := a.send(sctx, conn, Envelope{T: "m", D: s}); err != nil {
					a.requeue(batch[i:])
					return err
				}
			}
		}
		select {
		case <-ctx.Done():
			conn.Close(websocket.StatusGoingAway, "agent stopping")
			return nil
		case err := <-errc:
			return err
		case <-ping.C:
			pctx, c := context.WithTimeout(sctx, 15*time.Second)
			err := conn.Ping(pctx)
			c()
			if err != nil {
				return fmt.Errorf("ping: %w", err)
			}
		case <-a.wake:
		}
	}
}

func (a *Agent) send(ctx context.Context, conn *websocket.Conn, e Envelope) error {
	b, err := json.Marshal(e)
	if err != nil {
		return err
	}
	wctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	return conn.Write(wctx, websocket.MessageText, b)
}

func (a *Agent) reader(ctx context.Context, conn *websocket.Conn) error {
	for {
		_, b, err := conn.Read(ctx)
		if err != nil {
			return err
		}
		var in inbound
		if json.Unmarshal(b, &in) != nil {
			continue
		}
		switch in.T {
		case "cfg":
			if in.Interval >= 2 && in.Interval <= 300 {
				d := time.Duration(in.Interval) * time.Second
				if d != a.interval {
					a.interval = d
					select {
					case a.reset <- d:
					default:
					}
				}
			}
		case "req":
			// Phase 1 is read-only: the agent accepts no commands at all.
			// (Phase 2 adds an allow-list that lives in the agent's own
			// config, so even a compromised hub cannot exceed it.)
			_ = a.send(ctx, conn, Envelope{T: "res", ID: in.ID, D: map[string]string{
				"error": "unsupported operation: " + in.Op,
			}})
		}
	}
}

// Enroll trades a one-time token for a permanent identity.
func Enroll(hub, token, version string, host collect.Host) (*config.Config, error) {
	body, _ := json.Marshal(map[string]any{"token": token, "host": host, "version": version})
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Post(hub+"/api/agent/enroll", "application/json", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if resp.StatusCode != http.StatusOK {
		var e struct {
			Detail string `json:"detail"`
		}
		_ = json.Unmarshal(raw, &e)
		if e.Detail == "" {
			e.Detail = strings.TrimSpace(string(raw))
		}
		return nil, fmt.Errorf("enroll failed: HTTP %d: %s", resp.StatusCode, e.Detail)
	}
	var out struct {
		ServerID string `json:"server_id"`
		Secret   string `json:"secret"`
		Interval int    `json:"interval"`
	}
	if err := json.Unmarshal(raw, &out); err != nil || out.ServerID == "" || out.Secret == "" {
		return nil, fmt.Errorf("enroll: unexpected reply from hub")
	}
	return &config.Config{Hub: hub, ServerID: out.ServerID, Secret: out.Secret, Interval: out.Interval}, nil
}
