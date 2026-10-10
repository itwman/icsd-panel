package collect

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// Containers are listed through the Docker socket (read-only GET), while
// their CPU and memory come straight from cgroup files. `docker stats`
// would cost ~1s per container; cgroup files cost microseconds.

type dockerItem struct {
	ID     string   `json:"Id"`
	Names  []string `json:"Names"`
	Image  string   `json:"Image"`
	State  string   `json:"State"`
	Status string   `json:"Status"`
}

// dockerClient is created once and reused: a new Transport per call would
// leak one idle connection (and two goroutines) every refresh.
func (c *Collector) dockerClient() *http.Client {
	if c.docker == nil {
		sock := c.DockerSock
		c.docker = &http.Client{
			Timeout: 4 * time.Second,
			Transport: &http.Transport{
				DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
					var d net.Dialer
					return d.DialContext(ctx, "unix", sock)
				},
				MaxIdleConns:    1,
				IdleConnTimeout: 30 * time.Second,
			},
		}
	}
	return c.docker
}

func (c *Collector) readContainers(now time.Time) ([]Container, string) {
	if _, err := os.Stat(c.DockerSock); err != nil {
		return nil, "" // no Docker on this host: not an error
	}
	resp, err := c.dockerClient().Get("http://docker/containers/json?all=1")
	if err != nil {
		if errors.Is(err, os.ErrPermission) || strings.Contains(err.Error(), "permission denied") {
			return nil, "permission denied on docker.sock (agent user is not in the docker group)"
		}
		return nil, err.Error()
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Sprintf("docker API: HTTP %d", resp.StatusCode)
	}
	var items []dockerItem
	if err := json.NewDecoder(resp.Body).Decode(&items); err != nil {
		return nil, "docker API: " + err.Error()
	}

	dt := now.Sub(c.cgAt).Seconds()
	if c.cgAt.IsZero() {
		dt = 0
	}
	cur := map[string]uint64{}
	out := make([]Container, 0, len(items))
	for _, it := range items {
		ct := Container{
			ID:     shortID(it.ID),
			Image:  it.Image,
			State:  it.State,
			Status: it.Status,
			Health: healthOf(it.Status),
		}
		if len(it.Names) > 0 {
			ct.Name = strings.TrimPrefix(it.Names[0], "/")
		}
		if it.State == "running" {
			if usage, mem, ok := c.cgroupStats(it.ID); ok {
				cur[it.ID] = usage
				ct.Mem = mem
				if prev, ok := c.cgroups[it.ID]; ok && dt > 0 && usage >= prev {
					ct.CPU = round2(float64(usage-prev) / 1e6 / dt * 100)
				}
			}
		}
		out = append(out, ct)
	}
	c.cgroups, c.cgAt = cur, now
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, ""
}

func shortID(id string) string {
	if len(id) > 12 {
		return id[:12]
	}
	return id
}

func healthOf(status string) string {
	for _, h := range []string{"unhealthy", "healthy", "health: starting"} {
		if strings.Contains(status, "("+h+")") {
			return strings.TrimPrefix(h, "health: ")
		}
	}
	return ""
}

// cgroupStats returns cumulative CPU time (µs) and working-set memory.
func (c *Collector) cgroupStats(id string) (usageUS, mem uint64, ok bool) {
	cg := filepath.Join(c.Sys, "fs", "cgroup")
	// cgroup v2 (systemd driver, then cgroupfs driver)
	for _, dir := range []string{
		filepath.Join(cg, "system.slice", "docker-"+id+".scope"),
		filepath.Join(cg, "docker", id),
	} {
		stat := readLines(filepath.Join(dir, "cpu.stat"))
		if stat == nil {
			continue
		}
		for _, line := range stat {
			if v, ok := strings.CutPrefix(line, "usage_usec "); ok {
				usageUS = u64(v)
			}
		}
		mem = workingSet(u64(readString(filepath.Join(dir, "memory.current"))),
			filepath.Join(dir, "memory.stat"), "inactive_file ")
		return usageUS, mem, true
	}
	// cgroup v1
	for _, sub := range []string{filepath.Join("docker", id), filepath.Join("system.slice", "docker-"+id+".scope")} {
		ns := readString(filepath.Join(cg, "cpuacct", sub, "cpuacct.usage"))
		if ns == "" {
			continue
		}
		usageUS = u64(ns) / 1000
		mdir := filepath.Join(cg, "memory", sub)
		mem = workingSet(u64(readString(filepath.Join(mdir, "memory.usage_in_bytes"))),
			filepath.Join(mdir, "memory.stat"), "total_inactive_file ")
		return usageUS, mem, true
	}
	return 0, 0, false
}

// workingSet subtracts reclaimable page cache, matching `docker stats`.
func workingSet(usage uint64, statPath, key string) uint64 {
	for _, line := range readLines(statPath) {
		if v, ok := strings.CutPrefix(line, key); ok {
			if inactive := u64(v); inactive < usage {
				return usage - inactive
			}
		}
	}
	return usage
}
