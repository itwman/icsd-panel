package actions

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

// Runner executes requests from the hub. A nil *Runner (monitor-only mode:
// not root, or no policy file) refuses everything.
type Runner struct {
	Policy     *Policy
	DockerSock string

	mu   sync.Mutex
	jobs map[string]*Job
}

// JobView is what the hub sees.
type JobView struct {
	ID      string `json:"id"`
	Kind    string `json:"kind"`
	Title   string `json:"title"`
	State   string `json:"state"`
	Started int64  `json:"started"`
	Ended   int64  `json:"ended,omitempty"`
	Log     string `json:"log,omitempty"`
	Error   string `json:"error,omitempty"`
	Result  any    `json:"result,omitempty"`
}

type Job struct {
	ID      string `json:"id"`
	Kind    string `json:"kind"`
	Title   string `json:"title"`
	State   string `json:"state"` // running | done | error
	Started int64  `json:"started"`
	Ended   int64  `json:"ended,omitempty"`
	Log     string `json:"log,omitempty"`
	Error   string `json:"error,omitempty"`
	Result  any    `json:"result,omitempty"`

	mu  sync.Mutex
	log strings.Builder
}

func (j *Job) Logf(format string, args ...any) {
	j.mu.Lock()
	defer j.mu.Unlock()
	fmt.Fprintf(&j.log, format+"\n", args...)
	if j.log.Len() > 64*1024 { // keep the tail
		s := j.log.String()
		j.log.Reset()
		j.log.WriteString(s[len(s)-48*1024:])
	}
}

func (j *Job) snapshot() JobView {
	j.mu.Lock()
	defer j.mu.Unlock()
	return JobView{ID: j.ID, Kind: j.Kind, Title: j.Title, State: j.State, Started: j.Started,
		Ended: j.Ended, Log: j.log.String(), Error: j.Error, Result: j.Result}
}

// New returns a Runner when the agent may act, nil otherwise.
func New(policyPath, dockerSock string) (*Runner, error) {
	if os.Geteuid() != 0 {
		return nil, errors.New("monitor-only: agent is not running as root")
	}
	p, err := LoadPolicy(policyPath)
	if err != nil {
		return nil, err
	}
	if !p.Docker {
		dockerSock = ""
	}
	return &Runner{Policy: p, DockerSock: dockerSock, jobs: map[string]*Job{}}, nil
}

func (r *Runner) Caps() map[string]any {
	if r == nil {
		return map[string]any{"mode": "monitor"}
	}
	c := r.Policy.Caps()
	c["mode"] = "manage"
	if r.DockerSock == "" {
		c["docker"] = false
	}
	return c
}

type handler func(ctx context.Context, args json.RawMessage) (any, error)

func (r *Runner) handlers() map[string]handler {
	return map[string]handler{
		"fs.list": r.fsList, "fs.read": r.fsRead, "fs.write": r.fsWrite, "fs.mkdir": r.fsMkdir,
		"fs.rename": r.fsRename, "fs.delete": r.fsDelete, "fs.chmod": r.fsChmod, "fs.tail": r.fsTail,
		"fs.archive": r.fsArchive, "fs.abort": r.fsAbort, "fs.extract": r.fsExtract, "fs.stat": r.fsStat,
		"svc.list": r.svcList, "svc.action": r.svcAction, "svc.logs": r.svcLogs,
		"docker.action": r.dockerAction, "docker.logs": r.dockerLogs,
		"exec.run": r.execRun,
		"db.list":  r.dbList, "db.dump": r.dbDump, "db.restore": r.dbRestore,
		"backup.run": r.backupRun, "backup.list": r.backupList, "backup.prune": r.backupPrune,
		"ssl.list": r.sslList, "ssl.renew": r.sslRenew, "ssl.issue": r.sslIssue,
		"job.status": r.jobStatus, "job.list": r.jobList,
	}
}

// Handle runs one request. Errors are returned as plain messages for the panel.
func (r *Runner) Handle(ctx context.Context, op string, args json.RawMessage) (any, error) {
	if r == nil {
		return nil, errors.New("این سرور در حالت «فقط مانیتورینگ» است؛ برای مدیریت، agent را با --upgrade دوباره نصب کنید")
	}
	h, ok := r.handlers()[op]
	if !ok {
		return nil, fmt.Errorf("عملیات ناشناخته: %s", op)
	}
	if len(args) == 0 {
		args = json.RawMessage("{}")
	}
	return h(ctx, args)
}

func decode(args json.RawMessage, v any) error {
	if err := json.Unmarshal(args, v); err != nil {
		return fmt.Errorf("ورودی نامعتبر: %v", err)
	}
	return nil
}

// ── background jobs (dumps, archives, certbot…) ──────────────────────

func newID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// Start runs fn in the background and returns its id immediately.
func (r *Runner) Start(kind, title string, fn func(ctx context.Context, j *Job) (any, error)) map[string]any {
	j := &Job{ID: newID(), Kind: kind, Title: title, State: "running", Started: time.Now().Unix()}
	r.mu.Lock()
	r.gc()
	r.jobs[j.ID] = j
	r.mu.Unlock()
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 6*time.Hour)
		defer cancel()
		res, err := func() (res any, err error) {
			defer func() {
				if p := recover(); p != nil {
					err = fmt.Errorf("panic: %v", p)
				}
			}()
			return fn(ctx, j)
		}()
		j.mu.Lock()
		j.Ended = time.Now().Unix()
		if err != nil {
			j.State, j.Error = "error", err.Error()
		} else {
			j.State, j.Result = "done", res
		}
		j.mu.Unlock()
	}()
	return map[string]any{"job": j.ID}
}

func (r *Runner) gc() {
	cut := time.Now().Add(-6 * time.Hour).Unix()
	for id, j := range r.jobs {
		j.mu.Lock()
		old := j.Ended != 0 && j.Ended < cut
		j.mu.Unlock()
		if old {
			delete(r.jobs, id)
		}
	}
}

func (r *Runner) jobStatus(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		ID string `json:"id"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	r.mu.Lock()
	j, ok := r.jobs[in.ID]
	r.mu.Unlock()
	if !ok {
		return nil, errors.New("کار پیدا نشد (شاید agent دوباره راه‌اندازی شده)")
	}
	return j.snapshot(), nil
}

func (r *Runner) jobList(_ context.Context, _ json.RawMessage) (any, error) {
	r.mu.Lock()
	out := make([]JobView, 0, len(r.jobs))
	for _, j := range r.jobs {
		s := j.snapshot()
		s.Log = ""
		out = append(out, s)
	}
	r.mu.Unlock()
	sort.Slice(out, func(a, b int) bool { return out[a].Started > out[b].Started })
	return out, nil
}
