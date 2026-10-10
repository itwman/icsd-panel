package collect

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// fakeHost builds a minimal /proc and /sys tree.
func fakeHost(t *testing.T) (*Collector, string) {
	t.Helper()
	root := t.TempDir()
	c := &Collector{
		Proc:      filepath.Join(root, "proc"),
		Sys:       filepath.Join(root, "sys"),
		Passwd:    filepath.Join(root, "passwd"),
		SlowEvery: 1,
	}
	write(t, c.Proc, "loadavg", "0.50 0.40 0.30 2/345 999\n")
	write(t, c.Proc, "uptime", "3600.12 7000.00\n")
	write(t, c.Proc, "meminfo", "MemTotal:  4000000 kB\nMemFree: 500000 kB\nMemAvailable: 1000000 kB\nBuffers: 100000 kB\nCached: 900000 kB\nSwapTotal: 2000000 kB\nSwapFree: 1500000 kB\n")
	write(t, c.Proc, "net/sockstat", "sockets: used 300\nTCP: inuse 12 orphan 0 tw 3 alloc 20 mem 4\n")
	write(t, c.Proc, "net/sockstat6", "TCP6: inuse 3\n")
	write(t, c.Proc, "self/mounts", "")
	write(t, c.Passwd, "", "root:x:0:0::/root:/bin/bash\nwww:x:33:33::/:/bin/false\n")
	mkdir(t, c.Sys, "block/vda")
	mkdir(t, c.Sys, "devices/virtual/net/lo")
	mkdir(t, c.Sys, "devices/virtual/net/docker0")
	return c, root
}

func write(t *testing.T, base, rel, body string) {
	t.Helper()
	p := filepath.Join(base, rel)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func mkdir(t *testing.T, base, rel string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(base, rel), 0o755); err != nil {
		t.Fatal(err)
	}
}

func TestCPUFromDeltas(t *testing.T) {
	c, _ := fakeHost(t)
	write(t, c.Proc, "stat", "cpu  100 0 50 800 50 0 0 0 0 0\n")
	if got := c.readCPU(); got.Pct != 0 {
		t.Fatalf("first read must report 0, got %+v", got)
	}
	// +1000 ticks: user 300, system 100, idle 400, iowait 100, steal 100
	write(t, c.Proc, "stat", "cpu  400 0 150 1200 150 0 0 100 0 0\n")
	got := c.readCPU()
	want := CPU{Pct: 50, User: 30, System: 10, IOWait: 10, Steal: 10}
	if got != want {
		t.Fatalf("got %+v want %+v", got, want)
	}
}

func TestMemoryUsesAvailable(t *testing.T) {
	c, _ := fakeHost(t)
	m := c.readMem()
	if m.Total != 4000000*1024 || m.Used != 3000000*1024 || m.Pct != 75 {
		t.Fatalf("mem %+v", m)
	}
	if m.SwapUsed != 500000*1024 || m.SwapPct != 25 {
		t.Fatalf("swap %+v", m)
	}
}

func TestLoadProcsUptimeTCP(t *testing.T) {
	c, _ := fakeHost(t)
	load, procs := c.readLoad()
	if load != [3]float64{0.5, 0.4, 0.3} || procs != 345 {
		t.Fatalf("load %v procs %d", load, procs)
	}
	if c.readUptime() != 3600 {
		t.Fatal("uptime")
	}
	if c.readTCP() != 15 {
		t.Fatalf("tcp %d", c.readTCP())
	}
}

func TestNetSkipsVirtualInterfaces(t *testing.T) {
	c, _ := fakeHost(t)
	dev := func(eth, dock uint64) string {
		return "Inter-|   Receive\n face |bytes\n" +
			"    lo: 999999 0 0 0 0 0 0 0 999999 0 0 0 0 0 0 0\n" +
			"  eth0: " + itoa(eth) + " 0 0 0 0 0 0 0 " + itoa(eth/2) + " 0 0 0 0 0 0 0\n" +
			"docker0: " + itoa(dock) + " 0 0 0 0 0 0 0 " + itoa(dock) + " 0 0 0 0 0 0 0\n"
	}
	write(t, c.Proc, "net/dev", dev(1000, 5000))
	c.readNet(0)
	write(t, c.Proc, "net/dev", dev(11000, 900000))
	n := c.readNet(2)
	if n.RxBps != 5000 || n.TxBps != 2500 {
		t.Fatalf("net %+v (docker0/lo must be ignored)", n)
	}
}

func TestDiskIOWholeDisksOnly(t *testing.T) {
	c, _ := fakeHost(t)
	line := func(r, w uint64) string {
		return " 253 0 vda 1 0 " + itoa(r) + " 0 1 0 " + itoa(w) + " 0 0 0 0\n" +
			" 253 1 vda1 1 0 " + itoa(r) + " 0 1 0 " + itoa(w) + " 0 0 0 0\n" +
			"   7 0 loop0 1 0 99999 0 1 0 99999 0 0 0 0\n"
	}
	write(t, c.Proc, "diskstats", line(0, 0))
	c.readDiskIO(0)
	write(t, c.Proc, "diskstats", line(20, 40))
	io := c.readDiskIO(1)
	if io.ReadBps != 20*512 || io.WriteBps != 40*512 {
		t.Fatalf("io %+v (partitions and loops must not double count)", io)
	}
}

func TestCounterResetIsNotNegative(t *testing.T) {
	if rate(5, 100, 1) != 0 {
		t.Fatal("a counter that went backwards must give 0, not a huge number")
	}
}

func TestTopProcesses(t *testing.T) {
	c, _ := fakeHost(t)
	stat := func(name string, ticks, rssPages int) string {
		// pid (comm) state ppid … utime stime … rss is field 24
		return "1 (" + name + ") S 0 0 0 0 0 0 0 0 0 0 " + itoa(uint64(ticks)) + " 0 0 0 0 0 0 0 0 " + itoa(uint64(rssPages)) + " 0\n"
	}
	write(t, c.Proc, "10/stat", stat("post gres)x", 100, 1000))
	write(t, c.Proc, "10/status", "Name: postgres\nUid:\t33\t33\t33\t33\n")
	write(t, c.Proc, "20/stat", stat("caddy", 50, 10))
	write(t, c.Proc, "20/status", "Uid:\t0\t0\t0\t0\n")
	t0 := time.Unix(1000, 0)
	c.readTop(t0)
	write(t, c.Proc, "10/stat", stat("post gres)x", 300, 1000)) // 200 ticks in 2s = 100%
	top := c.readTop(t0.Add(2 * time.Second))
	if len(top) != 2 || top[0].Name != "post gres)x" || top[0].CPU != 100 || top[0].User != "www" {
		t.Fatalf("top %+v", top)
	}
	if top[1].User != "root" || top[1].CPU != 0 {
		t.Fatalf("top %+v", top)
	}
}

func TestDockerHealthParsing(t *testing.T) {
	for in, want := range map[string]string{
		"Up 3 hours (healthy)":            "healthy",
		"Up 2 minutes (unhealthy)":        "unhealthy",
		"Up 5 seconds (health: starting)": "starting",
		"Exited (1) 2 hours ago":          "",
	} {
		if got := healthOf(in); got != want {
			t.Errorf("%q → %q want %q", in, got, want)
		}
	}
}

func TestCgroupV2WorkingSet(t *testing.T) {
	c, _ := fakeHost(t)
	dir := "fs/cgroup/system.slice/docker-abc.scope"
	write(t, c.Sys, dir+"/cpu.stat", "usage_usec 5000000\nuser_usec 1\n")
	write(t, c.Sys, dir+"/memory.current", "300000000\n")
	write(t, c.Sys, dir+"/memory.stat", "anon 1\ninactive_file 100000000\n")
	u, m, ok := c.cgroupStats("abc")
	if !ok || u != 5000000 || m != 200000000 {
		t.Fatalf("u=%d m=%d ok=%v", u, m, ok)
	}
}

func TestCgroupV1(t *testing.T) {
	c, _ := fakeHost(t)
	write(t, c.Sys, "fs/cgroup/cpuacct/docker/abc/cpuacct.usage", "7000000000\n")
	write(t, c.Sys, "fs/cgroup/memory/docker/abc/memory.usage_in_bytes", "50\n")
	write(t, c.Sys, "fs/cgroup/memory/docker/abc/memory.stat", "total_inactive_file 20\n")
	u, m, ok := c.cgroupStats("abc")
	if !ok || u != 7000000 || m != 30 {
		t.Fatalf("u=%d m=%d ok=%v", u, m, ok)
	}
}

func TestUnescapeMount(t *testing.T) {
	if got := unescapeMount(`/mnt/my\040disk`); got != "/mnt/my disk" {
		t.Fatal(got)
	}
}

func TestRealHostSampleIsSane(t *testing.T) {
	if _, err := os.Stat("/proc/stat"); err != nil {
		t.Skip("not Linux")
	}
	c := New()
	c.DockerSock = ""
	c.Sample()
	time.Sleep(200 * time.Millisecond)
	s := c.Sample()
	if s.Mem.Total == 0 || s.Procs == 0 || s.Uptime == 0 {
		t.Fatalf("sample %+v", s)
	}
	if s.CPU.Pct < 0 || s.CPU.Pct > 100 {
		t.Fatalf("cpu %v", s.CPU.Pct)
	}
}

func itoa(v uint64) string {
	if v == 0 {
		return "0"
	}
	var b []byte
	for v > 0 {
		b = append([]byte{byte('0' + v%10)}, b...)
		v /= 10
	}
	return string(b)
}

func TestPrimeGivesTheFirstSampleRealRates(t *testing.T) {
	c, _ := fakeHost(t)
	c.DockerSock = ""
	write(t, c.Proc, "stat", "cpu  100 0 0 900 0 0 0 0 0 0\n")
	write(t, c.Proc, "net/dev", "x\ny\n  eth0: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
	c.Prime()
	write(t, c.Proc, "stat", "cpu  600 0 0 1400 0 0 0 0 0 0\n")
	s := c.Sample()
	if s.CPU.Pct != 50 {
		t.Fatalf("first sample after Prime must not report 0%% CPU: %+v", s.CPU)
	}
	if s.Disks == nil && c.n != 1 {
		t.Fatal("the first real sample must also be a slow (full) one")
	}
}

func TestDockerClientIsReused(t *testing.T) {
	c := New()
	if c.dockerClient() != c.dockerClient() {
		t.Fatal("a new client per refresh leaks a connection every 15 s")
	}
}
