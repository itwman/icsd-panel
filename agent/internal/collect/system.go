package collect

import (
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// ── CPU ─────────────────────────────────────────────────────────────────

type cpuTimes struct {
	user, nice, system, idle, iowait, irq, softirq, steal uint64
}

func (t cpuTimes) total() uint64 {
	return t.user + t.nice + t.system + t.idle + t.iowait + t.irq + t.softirq + t.steal
}

func (c *Collector) readCPU() CPU {
	var cur cpuTimes
	for _, line := range readLines(c.p("stat")) {
		f := strings.Fields(line)
		if len(f) < 9 || f[0] != "cpu" {
			continue
		}
		cur = cpuTimes{u64(f[1]), u64(f[2]), u64(f[3]), u64(f[4]), u64(f[5]), u64(f[6]), u64(f[7]), u64(f[8])}
		break
	}
	prev := c.cpu
	c.cpu = cur
	if prev.total() == 0 || cur.total() <= prev.total() {
		return CPU{}
	}
	d := float64(cur.total() - prev.total())
	sub := func(a, b uint64) float64 {
		if a < b {
			return 0
		}
		return float64(a - b)
	}
	idle := sub(cur.idle, prev.idle) + sub(cur.iowait, prev.iowait)
	return CPU{
		Pct:    pct(d-idle, d),
		User:   pct(sub(cur.user, prev.user)+sub(cur.nice, prev.nice), d),
		System: pct(sub(cur.system, prev.system)+sub(cur.irq, prev.irq)+sub(cur.softirq, prev.softirq), d),
		IOWait: pct(sub(cur.iowait, prev.iowait), d),
		Steal:  pct(sub(cur.steal, prev.steal), d),
	}
}

// ── load, processes, uptime, tcp ────────────────────────────────────────

func (c *Collector) readLoad() (load [3]float64, procs int) {
	f := strings.Fields(readString(c.p("loadavg")))
	if len(f) < 4 {
		return
	}
	for i := 0; i < 3; i++ {
		load[i], _ = strconv.ParseFloat(f[i], 64)
	}
	if _, total, ok := strings.Cut(f[3], "/"); ok {
		procs, _ = strconv.Atoi(total)
	}
	return
}

func (c *Collector) readUptime() int64 {
	f := strings.Fields(readString(c.p("uptime")))
	if len(f) == 0 {
		return 0
	}
	v, _ := strconv.ParseFloat(f[0], 64)
	return int64(v)
}

func (c *Collector) readTCP() int {
	n := 0
	for _, name := range []string{"sockstat", "sockstat6"} {
		for _, line := range readLines(c.p("net", name)) {
			f := strings.Fields(line)
			if len(f) >= 3 && (f[0] == "TCP:" || f[0] == "TCP6:") && f[1] == "inuse" {
				v, _ := strconv.Atoi(f[2])
				n += v
			}
		}
	}
	return n
}

// ── memory ──────────────────────────────────────────────────────────────

func (c *Collector) readMem() Mem {
	kv := map[string]uint64{}
	for _, line := range readLines(c.p("meminfo")) {
		k, v, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		f := strings.Fields(v)
		if len(f) == 0 {
			continue
		}
		kv[k] = u64(f[0]) * 1024
	}
	m := Mem{Total: kv["MemTotal"], SwapTotal: kv["SwapTotal"]}
	avail, ok := kv["MemAvailable"]
	if !ok { // kernels older than 3.14
		avail = kv["MemFree"] + kv["Buffers"] + kv["Cached"]
	}
	if avail > m.Total {
		avail = m.Total
	}
	m.Available = avail
	m.Used = m.Total - avail
	m.Cached = kv["Cached"] + kv["Buffers"]
	m.Pct = pct(float64(m.Used), float64(m.Total))
	if free := kv["SwapFree"]; free <= m.SwapTotal {
		m.SwapUsed = m.SwapTotal - free
	}
	m.SwapPct = pct(float64(m.SwapUsed), float64(m.SwapTotal))
	return m
}

// ── disk I/O (whole disks only, not partitions) ─────────────────────────

func (c *Collector) isWholeDisk(dev string) bool {
	if strings.HasPrefix(dev, "loop") || strings.HasPrefix(dev, "ram") ||
		strings.HasPrefix(dev, "zram") || strings.HasPrefix(dev, "dm-") || strings.HasPrefix(dev, "sr") {
		return false
	}
	// /sys/block lists whole disks; partitions live under them.
	_, err := os.Stat(filepath.Join(c.Sys, "block", dev))
	return err == nil
}

func (c *Collector) readDiskIO(dt float64) IO {
	cur := map[string][2]uint64{}
	for _, line := range readLines(c.p("diskstats")) {
		f := strings.Fields(line)
		if len(f) < 10 || !c.isWholeDisk(f[2]) {
			continue
		}
		cur[f[2]] = [2]uint64{u64(f[5]), u64(f[9])}
	}
	var io IO
	for dev, v := range cur {
		if p, ok := c.disk[dev]; ok {
			io.ReadBps += rate(v[0]*512, p[0]*512, dt)
			io.WriteBps += rate(v[1]*512, p[1]*512, dt)
		}
	}
	c.disk = cur
	io.ReadBps, io.WriteBps = round2(io.ReadBps), round2(io.WriteBps)
	return io
}

// ── network (physical interfaces only) ──────────────────────────────────

func (c *Collector) isVirtualIface(name string) bool {
	if name == "lo" {
		return true
	}
	// Docker bridges, veths, tunnels… all live under /sys/devices/virtual/net.
	_, err := os.Stat(filepath.Join(c.Sys, "devices", "virtual", "net", name))
	return err == nil
}

func (c *Collector) readNet(dt float64) Net {
	cur := map[string][2]uint64{}
	for _, line := range readLines(c.p("net", "dev")) {
		name, rest, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		name = strings.TrimSpace(name)
		f := strings.Fields(rest)
		if len(f) < 9 || c.isVirtualIface(name) {
			continue
		}
		cur[name] = [2]uint64{u64(f[0]), u64(f[8])}
	}
	var n Net
	for name, v := range cur {
		if p, ok := c.net[name]; ok {
			n.RxBps += rate(v[0], p[0], dt)
			n.TxBps += rate(v[1], p[1], dt)
		}
	}
	c.net = cur
	n.RxBps, n.TxBps = round2(n.RxBps), round2(n.TxBps)
	return n
}

// ── filesystems ─────────────────────────────────────────────────────────

var realFS = map[string]bool{
	"ext2": true, "ext3": true, "ext4": true, "xfs": true, "btrfs": true,
	"zfs": true, "f2fs": true, "reiserfs": true, "jfs": true, "vfat": false,
}

func (c *Collector) readDisks() []Disk {
	seen := map[string]bool{}
	var out []Disk
	for _, line := range readLines(c.p("self", "mounts")) {
		f := strings.Fields(line)
		if len(f) < 3 || !realFS[f[2]] {
			continue
		}
		dev, mount := f[0], unescapeMount(f[1])
		// Docker bind-mounts the same device many times; keep the shortest path.
		if seen[dev] {
			continue
		}
		var st syscall.Statfs_t
		if err := syscall.Statfs(mount, &st); err != nil || st.Blocks == 0 {
			continue
		}
		seen[dev] = true
		bs := uint64(st.Bsize)
		total := st.Blocks * bs
		free := st.Bavail * bs
		used := total - st.Bfree*bs
		d := Disk{
			Mount: mount, Device: dev, FS: f[2],
			Total: total, Used: used, Free: free,
			// Same formula as df: used / (used + available).
			Pct: pct(float64(used), float64(used+free)),
		}
		if st.Files > 0 {
			d.InodesPct = pct(float64(st.Files-st.Ffree), float64(st.Files))
		}
		out = append(out, d)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Mount < out[j].Mount })
	return out
}

func unescapeMount(s string) string {
	// /proc/mounts escapes spaces etc. as \040.
	if !strings.Contains(s, `\`) {
		return s
	}
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' && i+3 < len(s) {
			if v, err := strconv.ParseUint(s[i+1:i+4], 8, 8); err == nil {
				b.WriteByte(byte(v))
				i += 3
				continue
			}
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

// ── static host info ────────────────────────────────────────────────────

func (c *Collector) Host() Host {
	h := Host{Arch: runtime.GOARCH, CPUs: runtime.NumCPU()}
	h.Hostname, _ = os.Hostname()
	h.Kernel = readString(c.p("sys", "kernel", "osrelease"))
	for _, line := range readLines("/etc/os-release") {
		if v, ok := strings.CutPrefix(line, "PRETTY_NAME="); ok {
			h.OS = strings.Trim(v, `"`)
		}
	}
	cores := 0
	for _, line := range readLines(c.p("cpuinfo")) {
		if k, v, ok := strings.Cut(line, ":"); ok {
			k = strings.TrimSpace(k)
			if k == "model name" && h.CPUModel == "" {
				h.CPUModel = strings.TrimSpace(v)
			}
			if k == "processor" {
				cores++
			}
		}
	}
	if cores > 0 {
		h.CPUs = cores
	}
	h.MemTotal = c.readMem().Total
	if up := c.readUptime(); up > 0 {
		h.BootTime = time.Now().Unix() - up
	}
	h.Virt = readString(filepath.Join(c.Sys, "class", "dmi", "id", "sys_vendor"))
	return h
}
