package collect

import (
	"os"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	clockTicks = 100 // USER_HZ; 100 on every mainstream Linux build
	topN       = 8
)

var pageSize = uint64(os.Getpagesize())

// readTop returns the busiest processes by CPU, plus the biggest by memory.
// Only the short command name (comm) is sent, never the command line —
// command lines can carry passwords (`mysql -pSECRET`).
func (c *Collector) readTop(now time.Time) []Proc {
	entries, err := os.ReadDir(c.Proc)
	if err != nil {
		return nil
	}
	dt := now.Sub(c.procsAt).Seconds()
	if c.procsAt.IsZero() {
		dt = 0
	}
	cur := make(map[int]uint64, len(entries))
	var all []Proc
	for _, e := range entries {
		pid, err := strconv.Atoi(e.Name())
		if err != nil {
			continue
		}
		raw, err := os.ReadFile(c.p(e.Name(), "stat"))
		if err != nil {
			continue
		}
		name, fields, ok := parseStat(string(raw))
		if !ok || len(fields) < 22 {
			continue
		}
		// fields[0] is field 3 (state); utime=14, stime=15, rss=24.
		ticks := u64(fields[11]) + u64(fields[12])
		rss := u64(fields[21]) * pageSize
		cur[pid] = ticks
		p := Proc{PID: pid, Name: name, RSS: rss}
		if prev, ok := c.procs[pid]; ok && dt > 0 && ticks >= prev {
			p.CPU = round2(float64(ticks-prev) / clockTicks / dt * 100)
		}
		all = append(all, p)
	}
	c.procs, c.procsAt = cur, now

	pick := map[int]bool{}
	var out []Proc
	add := func(list []Proc) {
		for i := 0; i < len(list) && i < topN; i++ {
			if !pick[list[i].PID] {
				pick[list[i].PID] = true
				out = append(out, list[i])
			}
		}
	}
	sort.Slice(all, func(i, j int) bool { return all[i].CPU > all[j].CPU })
	add(all)
	sort.Slice(all, func(i, j int) bool { return all[i].RSS > all[j].RSS })
	add(all)
	for i := range out {
		out[i].User = c.userOf(out[i].PID)
	}
	return out
}

// parseStat splits /proc/PID/stat; the name may contain spaces and ')'.
func parseStat(s string) (name string, rest []string, ok bool) {
	open := strings.IndexByte(s, '(')
	close := strings.LastIndexByte(s, ')')
	if open < 0 || close < open {
		return "", nil, false
	}
	return s[open+1 : close], strings.Fields(s[close+1:]), true
}

func (c *Collector) userOf(pid int) string {
	uid := -1
	for _, line := range readLines(c.p(strconv.Itoa(pid), "status")) {
		if v, ok := strings.CutPrefix(line, "Uid:"); ok {
			if f := strings.Fields(v); len(f) > 0 {
				uid, _ = strconv.Atoi(f[0])
			}
			break
		}
	}
	if uid < 0 {
		return ""
	}
	if c.users == nil {
		c.users = map[int]string{}
		for _, line := range readLines(c.Passwd) {
			f := strings.Split(line, ":")
			if len(f) > 2 {
				if id, err := strconv.Atoi(f[2]); err == nil {
					c.users[id] = f[0]
				}
			}
		}
	}
	if n, ok := c.users[uid]; ok {
		return n
	}
	return strconv.Itoa(uid) // a container user unknown to the host
}
