// Package collect reads a Linux host's vital signs straight from /proc and
// /sys — no external tools, no cgo, no third-party libraries.
//
// همهٔ اعداد از /proc و /sys خوانده می‌شوند؛ هیچ دستوری اجرا نمی‌شود.
// هزینهٔ هر نمونه در حدِ چند صد میکروثانیه است.
package collect

// Sample is one snapshot sent to the hub. Rates (bps) are averaged over the
// interval since the previous sample; the very first sample reports zero rates.
type Sample struct {
	TS     int64      `json:"ts"` // unix milliseconds
	CPU    CPU        `json:"cpu"`
	Load   [3]float64 `json:"load"`
	Mem    Mem        `json:"mem"`
	IO     IO         `json:"io"`
	Net    Net        `json:"net"`
	TCP    int        `json:"tcp"`
	Procs  int        `json:"procs"`
	Uptime int64      `json:"uptime"`

	// Slower-moving parts: present only when refreshed (every few samples).
	Disks []Disk `json:"disks,omitempty"`
	Top   []Proc `json:"top,omitempty"`
	// Not omitempty: an empty list («Docker has no containers now») must
	// reach the hub so alerts of removed containers can resolve. null means
	// «not refreshed in this sample» or «no Docker».
	Containers []Container `json:"containers"`
	DockerErr  string      `json:"docker_err,omitempty"`
}

type CPU struct {
	Pct    float64 `json:"pct"` // everything except idle+iowait
	User   float64 `json:"user"`
	System float64 `json:"system"`
	IOWait float64 `json:"iowait"`
	Steal  float64 `json:"steal"` // a VPS whose neighbours eat its CPU
}

type Mem struct {
	Total     uint64  `json:"total"`
	Used      uint64  `json:"used"` // total - available
	Available uint64  `json:"available"`
	Cached    uint64  `json:"cached"`
	Pct       float64 `json:"pct"`
	SwapTotal uint64  `json:"swap_total"`
	SwapUsed  uint64  `json:"swap_used"`
	SwapPct   float64 `json:"swap_pct"`
}

type IO struct {
	ReadBps  float64 `json:"read_bps"`
	WriteBps float64 `json:"write_bps"`
}

type Net struct {
	RxBps float64 `json:"rx_bps"`
	TxBps float64 `json:"tx_bps"`
}

type Disk struct {
	Mount     string  `json:"mount"`
	Device    string  `json:"device"`
	FS        string  `json:"fs"`
	Total     uint64  `json:"total"`
	Used      uint64  `json:"used"`
	Free      uint64  `json:"free"` // available to unprivileged users
	Pct       float64 `json:"pct"`
	InodesPct float64 `json:"inodes_pct"`
}

type Proc struct {
	PID  int     `json:"pid"`
	Name string  `json:"name"`
	User string  `json:"user"`
	CPU  float64 `json:"cpu"` // percent of ONE core, like top
	RSS  uint64  `json:"rss"`
}

type Container struct {
	ID     string  `json:"id"`
	Name   string  `json:"name"`
	Image  string  `json:"image"`
	State  string  `json:"state"`  // running, exited, restarting…
	Health string  `json:"health"` // healthy, unhealthy, starting, ""
	Status string  `json:"status"` // "Up 3 hours (healthy)"
	CPU    float64 `json:"cpu"`    // percent of ONE core
	Mem    uint64  `json:"mem"`
}

// Host is static information sent once per connection.
type Host struct {
	Hostname string `json:"hostname"`
	OS       string `json:"os"`
	Kernel   string `json:"kernel"`
	Arch     string `json:"arch"`
	CPUs     int    `json:"cpus"`
	CPUModel string `json:"cpu_model"`
	MemTotal uint64 `json:"mem_total"`
	BootTime int64  `json:"boot_time"`
	Virt     string `json:"virt,omitempty"`
}
