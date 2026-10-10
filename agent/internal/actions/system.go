package actions

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os/exec"
	"regexp"
	"strings"
	"time"
)

// run executes a command (never through a shell unless asked) with a timeout
// and returns combined output, capped.
func run(ctx context.Context, timeout time.Duration, name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Env = append(cmd.Environ(), "LC_ALL=C", "SYSTEMD_COLORS=0", "SYSTEMD_PAGER=")
	var buf bytes.Buffer
	cmd.Stdout = &limited{w: &buf, n: 1 << 20}
	cmd.Stderr = cmd.Stdout
	err := cmd.Run()
	out := strings.TrimRight(buf.String(), "\n")
	if ctx.Err() == context.DeadlineExceeded {
		return out, fmt.Errorf("زمان اجرا تمام شد (%s)", timeout)
	}
	if err != nil {
		if out != "" {
			return out, fmt.Errorf("%s", lastLines(out, 6))
		}
		return out, err
	}
	return out, nil
}

type limited struct {
	w io.Writer
	n int
}

func (l *limited) Write(p []byte) (int, error) {
	if l.n <= 0 {
		return len(p), nil
	}
	q := p
	if len(q) > l.n {
		q = q[:l.n]
	}
	l.n -= len(q)
	_, err := l.w.Write(q)
	return len(p), err
}

func lastLines(s string, n int) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

// ── systemd services ─────────────────────────────────────────────────

type Service struct {
	Name    string `json:"name"`
	Load    string `json:"load"`
	Active  string `json:"active"`
	Sub     string `json:"sub"`
	Enabled string `json:"enabled"`
	Desc    string `json:"desc"`
}

func (r *Runner) svcList(ctx context.Context, _ json.RawMessage) (any, error) {
	out, err := run(ctx, 15*time.Second, "systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain", "--no-pager")
	if err != nil {
		return nil, err
	}
	enabled := map[string]string{}
	if uf, err := run(ctx, 15*time.Second, "systemctl", "list-unit-files", "--type=service", "--no-legend", "--plain", "--no-pager"); err == nil {
		for _, line := range strings.Split(uf, "\n") {
			f := strings.Fields(line)
			if len(f) >= 2 {
				enabled[f[0]] = f[1]
			}
		}
	}
	var list []Service
	for _, line := range strings.Split(out, "\n") {
		f := strings.Fields(line)
		if len(f) < 4 || !strings.HasSuffix(f[0], ".service") || !r.Policy.ServiceAllowed(f[0]) {
			continue
		}
		if f[1] == "not-found" {
			continue
		}
		list = append(list, Service{Name: strings.TrimSuffix(f[0], ".service"), Load: f[1], Active: f[2],
			Sub: f[3], Desc: strings.Join(f[4:], " "), Enabled: enabled[f[0]]})
	}
	return list, nil
}

var unitName = regexp.MustCompile(`^[A-Za-z0-9@._-]{1,128}$`)

func (r *Runner) svcAction(ctx context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Name   string `json:"name"`
		Action string `json:"action"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	switch in.Action {
	case "start", "stop", "restart", "reload", "enable", "disable":
	default:
		return nil, errors.New("کار نامعتبر")
	}
	if !unitName.MatchString(in.Name) || !r.Policy.ServiceAllowed(in.Name) {
		return nil, errors.New("این سرویس در فهرست مجاز این سرور نیست")
	}
	if _, err := run(ctx, 90*time.Second, "systemctl", in.Action, in.Name+".service"); err != nil {
		return nil, err
	}
	st, _ := run(ctx, 10*time.Second, "systemctl", "is-active", in.Name+".service")
	return map[string]any{"name": in.Name, "active": strings.TrimSpace(st)}, nil
}

func (r *Runner) svcLogs(ctx context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Name  string `json:"name"`
		Lines int    `json:"lines"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	if !unitName.MatchString(in.Name) || !r.Policy.ServiceAllowed(in.Name) {
		return nil, errors.New("این سرویس در فهرست مجاز این سرور نیست")
	}
	if in.Lines <= 0 || in.Lines > 3000 {
		in.Lines = 300
	}
	out, err := run(ctx, 20*time.Second, "journalctl", "-u", in.Name+".service", "-n", fmt.Sprint(in.Lines),
		"--no-pager", "-o", "short-iso")
	if err != nil && out == "" {
		return nil, err
	}
	return map[string]any{"lines": strings.Split(out, "\n")}, nil
}

// ── shell console (only if policy.console is true) ───────────────────

func (r *Runner) execRun(ctx context.Context, args json.RawMessage) (any, error) {
	if !r.Policy.Console {
		return nil, errors.New("کنسول روی این سرور خاموش است (\"console\": true در policy.json)")
	}
	var in struct {
		Cmd     string `json:"cmd"`
		Cwd     string `json:"cwd"`
		Timeout int    `json:"timeout"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	if strings.TrimSpace(in.Cmd) == "" {
		return nil, errors.New("دستور خالی است")
	}
	if in.Timeout <= 0 || in.Timeout > 300 {
		in.Timeout = 60
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(in.Timeout)*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "/bin/bash", "-c", in.Cmd)
	if in.Cwd != "" {
		cmd.Dir = in.Cwd
	}
	cmd.Env = append(cmd.Environ(), "TERM=dumb", "SYSTEMD_PAGER=", "PAGER=cat")
	var buf bytes.Buffer
	cmd.Stdout = &limited{w: &buf, n: 512 * 1024}
	cmd.Stderr = cmd.Stdout
	start := time.Now()
	err := cmd.Run()
	code := 0
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			code = ee.ExitCode()
		} else {
			code = -1
		}
	}
	if ctx.Err() == context.DeadlineExceeded {
		code = 124
	}
	return map[string]any{"output": buf.String(), "code": code, "ms": time.Since(start).Milliseconds()}, nil
}

// ── Docker: start/stop/restart and logs ──────────────────────────────

func (r *Runner) docker() (*http.Client, error) {
	if r.DockerSock == "" {
		return nil, errors.New("Docker روی این سرور در دسترس نیست یا در policy خاموش است")
	}
	sock := r.DockerSock
	return &http.Client{Timeout: 120 * time.Second, Transport: &http.Transport{
		DisableKeepAlives: true,
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", sock)
		}}}, nil
}

var containerRef = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)

func (r *Runner) dockerAction(ctx context.Context, args json.RawMessage) (any, error) {
	var in struct {
		ID     string `json:"id"`
		Action string `json:"action"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	if in.Action != "start" && in.Action != "stop" && in.Action != "restart" {
		return nil, errors.New("کار نامعتبر")
	}
	if !containerRef.MatchString(in.ID) {
		return nil, errors.New("شناسهٔ کانتینر نامعتبر")
	}
	c, err := r.docker()
	if err != nil {
		return nil, err
	}
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost,
		"http://docker/containers/"+url.PathEscape(in.ID)+"/"+in.Action+"?t=20", nil)
	resp, err := c.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if resp.StatusCode >= 300 && resp.StatusCode != http.StatusNotModified {
		return nil, fmt.Errorf("docker: %s", strings.TrimSpace(string(body)))
	}
	return map[string]any{"id": in.ID, "action": in.Action}, nil
}

func (r *Runner) dockerLogs(ctx context.Context, args json.RawMessage) (any, error) {
	var in struct {
		ID    string `json:"id"`
		Lines int    `json:"lines"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	if !containerRef.MatchString(in.ID) {
		return nil, errors.New("شناسهٔ کانتینر نامعتبر")
	}
	if in.Lines <= 0 || in.Lines > 3000 {
		in.Lines = 300
	}
	c, err := r.docker()
	if err != nil {
		return nil, err
	}
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, fmt.Sprintf(
		"http://docker/containers/%s/logs?stdout=1&stderr=1&timestamps=1&tail=%d", url.PathEscape(in.ID), in.Lines), nil)
	resp, err := c.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("docker: %s", strings.TrimSpace(string(raw)))
	}
	text := demux(raw)
	return map[string]any{"lines": strings.Split(strings.TrimRight(text, "\n"), "\n")}, nil
}

// demux strips Docker's 8-byte stream headers (absent for TTY containers).
func demux(b []byte) string {
	if len(b) < 8 || (b[0] != 1 && b[0] != 2) || b[1] != 0 || b[2] != 0 || b[3] != 0 {
		return string(b)
	}
	var out bytes.Buffer
	for len(b) >= 8 {
		n := int(binary.BigEndian.Uint32(b[4:8]))
		b = b[8:]
		if n > len(b) {
			n = len(b)
		}
		out.Write(b[:n])
		b = b[n:]
	}
	return out.String()
}
