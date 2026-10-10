package collect

import (
	"bufio"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Collector keeps the previous counters so every sample can report rates.
// Not safe for concurrent use; the agent samples from one goroutine.
type Collector struct {
	Proc       string // normally /proc
	Sys        string // normally /sys
	DockerSock string // "" disables Docker
	Passwd     string // normally /etc/passwd

	// SlowEvery: disks, top processes and containers are refreshed every
	// N samples (they change slowly and cost more than the rest).
	SlowEvery int

	n       int
	lastAt  time.Time
	cpu     cpuTimes
	disk    map[string][2]uint64 // device -> sectors read, written
	net     map[string][2]uint64 // iface -> rx, tx bytes
	procs   map[int]uint64       // pid -> utime+stime ticks
	procsAt time.Time
	cgroups map[string]uint64 // container id -> cpu usage µs
	cgAt    time.Time
	users   map[int]string
	docker  *http.Client
}

func New() *Collector {
	return &Collector{
		Proc:       "/proc",
		Sys:        "/sys",
		DockerSock: "/var/run/docker.sock",
		Passwd:     "/etc/passwd",
		SlowEvery:  3,
	}
}

// Prime reads every counter once without producing a sample, so the first
// real sample already has meaningful rates (CPU 0% right after an agent
// restart would otherwise close a CPU alert that is still true).
func (c *Collector) Prime() {
	now := time.Now()
	c.readCPU()
	c.readDiskIO(0)
	c.readNet(0)
	c.readTop(now)
	if c.DockerSock != "" {
		c.readContainers(now)
	}
	c.lastAt = now
}

// Sample takes one snapshot.
func (c *Collector) Sample() Sample {
	now := time.Now()
	dt := now.Sub(c.lastAt).Seconds()
	first := c.lastAt.IsZero()
	if first || dt <= 0 {
		dt = 0
	}
	s := Sample{TS: now.UnixMilli()}

	s.CPU = c.readCPU()
	s.Load, s.Procs = c.readLoad()
	s.Mem = c.readMem()
	s.IO = c.readDiskIO(dt)
	s.Net = c.readNet(dt)
	s.TCP = c.readTCP()
	s.Uptime = c.readUptime()

	if c.SlowEvery < 1 {
		c.SlowEvery = 1
	}
	if c.n%c.SlowEvery == 0 {
		s.Disks = c.readDisks()
		s.Top = c.readTop(now)
		if c.DockerSock != "" {
			s.Containers, s.DockerErr = c.readContainers(now)
		}
	}
	c.n++
	c.lastAt = now
	return s
}

// ── tiny helpers ────────────────────────────────────────────────────────

func (c *Collector) p(parts ...string) string {
	return filepath.Join(append([]string{c.Proc}, parts...)...)
}

func readLines(path string) []string {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	var out []string
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	for sc.Scan() {
		out = append(out, sc.Text())
	}
	return out
}

func readString(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

func u64(s string) uint64 {
	v, _ := strconv.ParseUint(strings.TrimSpace(s), 10, 64)
	return v
}

func pct(part, whole float64) float64 {
	if whole <= 0 {
		return 0
	}
	v := part / whole * 100
	if v < 0 {
		return 0
	}
	if v > 100 {
		return 100
	}
	return round2(v)
}

func round2(v float64) float64 {
	return float64(int64(v*100+0.5)) / 100
}

func rate(cur, prev uint64, dt float64) float64 {
	if dt <= 0 || cur < prev { // first sample, or counter reset
		return 0
	}
	return round2(float64(cur-prev) / dt)
}
