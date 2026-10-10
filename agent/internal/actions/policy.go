// Package actions is everything the agent can DO (phases 2–4): files,
// services, containers, logs, databases, backups, certificates.
//
// The limits live on the managed server itself, in
// /etc/icsd-agent/policy.json, which only root on that server can edit.
// The hub cannot change them: a compromised panel can do no more than what
// each server's own policy allows.
//
// محدودیت‌ها روی خودِ هر سرور تعریف می‌شوند، نه در پنل؛ پنلِ نفوذشده هم
// نمی‌تواند از فهرستِ مجازِ هر سرور فراتر برود.
package actions

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"strings"
)

const DefaultPolicyPath = "/etc/icsd-agent/policy.json"

type Policy struct {
	Files struct {
		Roots    []string `json:"roots"`    // read + write
		ReadOnly []string `json:"readonly"` // read only (logs, nginx config…)
		MaxMB    int      `json:"max_upload_mb"`
	} `json:"files"`
	Services  []string `json:"services"`      // glob patterns of units that may be managed
	DenySvc   []string `json:"deny_services"` // never, even if matched above
	Docker    bool     `json:"docker"`
	Databases bool     `json:"databases"`
	SSL       bool     `json:"ssl"`
	Console   bool     `json:"console"` // run shell commands (off by default)
	BackupDir string   `json:"backup_dir"`
}

// Paths that are never touched, whatever the policy says: the agent's own
// identity and policy (or the panel could widen its own rights), and SSH
// keys (or the panel could plant a permanent backdoor).
var alwaysDenied = []string{"/etc/icsd-agent", "/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/sudoers.d"}

func LoadPolicy(file string) (*Policy, error) {
	b, err := os.ReadFile(file)
	if err != nil {
		return nil, err
	}
	var p Policy
	if err := json.Unmarshal(b, &p); err != nil {
		return nil, fmt.Errorf("%s: %w", file, err)
	}
	p.normalize()
	return &p, nil
}

func (p *Policy) normalize() {
	clean := func(in []string) []string {
		var out []string
		for _, r := range in {
			if r = strings.TrimSpace(r); r != "" && filepath.IsAbs(r) {
				out = append(out, filepath.Clean(r))
			}
		}
		return out
	}
	p.Files.Roots = clean(p.Files.Roots)
	p.Files.ReadOnly = clean(p.Files.ReadOnly)
	if p.Files.MaxMB <= 0 {
		p.Files.MaxMB = 512
	}
	if p.BackupDir == "" {
		p.BackupDir = "/var/backups/icsd"
	}
	p.BackupDir = filepath.Clean(p.BackupDir)
	p.DenySvc = append(p.DenySvc, "ssh*", "sshd*", "icsd-agent*", "systemd-*", "dbus*", "getty*", "user@*",
		"networking*", "debug-shell*", "ufw*", "fail2ban*", "auditd*", "apparmor*", "firewalld*", "nftables*",
		"iptables*", "netfilter*", "polkit*", "cloud-init*", "sudo*", "emergency*", "rescue*", "serial-getty*")
}

// Caps tells the hub which tabs to show.
func (p *Policy) Caps() map[string]any {
	return map[string]any{
		"files":      append(append([]string{}, p.Files.Roots...), p.BackupDir),
		"readonly":   p.Files.ReadOnly,
		"max_mb":     p.Files.MaxMB,
		"services":   len(p.Services) > 0,
		"docker":     p.Docker,
		"databases":  p.Databases,
		"ssl":        p.SSL,
		"console":    p.Console,
		"backups":    true,
		"backup_dir": p.BackupDir,
	}
}

var errDenied = errors.New("خارج از مسیرهای مجاز این سرور (policy.json)")

func within(p, root string) bool {
	return p == root || strings.HasPrefix(p, strings.TrimSuffix(root, "/")+"/")
}

// resolve returns the real path, following symlinks of the existing part,
// so a link inside an allowed root cannot point outside it.
func resolve(p string) (string, error) {
	if !filepath.IsAbs(p) {
		return "", errors.New("مسیر باید مطلق باشد")
	}
	p = filepath.Clean(p)
	rest := ""
	cur := p
	for {
		real, err := filepath.EvalSymlinks(cur)
		if err == nil {
			return filepath.Join(real, rest), nil
		}
		if !os.IsNotExist(err) {
			return "", err
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			return p, nil
		}
		rest = filepath.Join(filepath.Base(cur), rest)
		cur = parent
	}
}

// Check validates a path for reading or writing and returns its real form.
func (p *Policy) Check(raw string, write bool) (string, error) {
	real, err := resolve(raw)
	if err != nil {
		return "", err
	}
	if deniedLexical(real) {
		return "", errDenied
	}
	if write && homeDotfile(real) {
		return "", errors.New("فایل‌های پنهانِ پوشهٔ خانهٔ کاربران (.bashrc، .profile…) از پنل نوشتنی نیستند")
	}
	roots := append(append([]string{}, p.Files.Roots...), p.BackupDir)
	for _, r := range roots {
		if rr, err := resolve(r); err == nil && within(real, rr) {
			return real, nil // (roots themselves can't be deleted/renamed: see IsRoot)
		}
	}
	if !write {
		for _, r := range p.Files.ReadOnly {
			if rr, err := resolve(r); err == nil && within(real, rr) {
				return real, nil
			}
		}
	}
	return "", errDenied
}

// deniedLexical: paths never read or written, whatever the roots.
func deniedLexical(p string) bool {
	for _, d := range alwaysDenied {
		if within(p, d) {
			return true
		}
	}
	for _, part := range strings.Split(p, "/") {
		if part == ".ssh" || part == ".gnupg" {
			return true
		}
	}
	return false
}

// homeDotfile: /home/<user>/.<anything> — shell start-up files and the like,
// which would let the panel run code as that user at their next login.
func homeDotfile(p string) bool {
	parts := strings.Split(p, "/")
	return len(parts) >= 4 && parts[1] == "home" && strings.HasPrefix(parts[3], ".")
}

// CheckEntry validates a path for delete/rename: the entry itself (a symlink
// stays a symlink) inside a writable directory, and never a root.
func (p *Policy) CheckEntry(raw string) (string, error) {
	if !filepath.IsAbs(raw) {
		return "", errors.New("مسیر باید مطلق باشد")
	}
	clean := filepath.Clean(raw)
	base := filepath.Base(clean)
	if base == "/" || base == "." || base == ".." {
		return "", errDenied
	}
	parent, err := p.Check(filepath.Dir(clean), true)
	if err != nil {
		return "", err
	}
	entry := filepath.Join(parent, base)
	if deniedLexical(entry) || homeDotfile(entry) {
		return "", errDenied
	}
	if p.IsRoot(entry) {
		return "", errors.New("پوشه‌های اصلی را نمی‌شود حذف یا جابه‌جا کرد")
	}
	return entry, nil
}

// IsRoot reports whether a path is one of the configured roots (which must
// not be deleted or renamed through the panel).
func (p *Policy) IsRoot(real string) bool {
	for _, r := range append(append([]string{}, p.Files.Roots...), p.Files.ReadOnly...) {
		if rr, err := resolve(r); err == nil && rr == real {
			return true
		}
	}
	rr, _ := resolve(p.BackupDir)
	return rr == real || real == "/"
}

// ServiceAllowed matches a unit name ("nginx.service" or "nginx") against
// the allow and deny patterns.
func (p *Policy) ServiceAllowed(unit string) bool {
	name := strings.TrimSuffix(unit, ".service")
	if name == "" || strings.ContainsAny(name, "/ ") {
		return false
	}
	for _, d := range p.DenySvc {
		if ok, _ := path.Match(d, name); ok {
			return false
		}
	}
	for _, a := range p.Services {
		if ok, _ := path.Match(a, name); ok {
			return true
		}
	}
	return false
}
