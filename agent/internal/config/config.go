// Package config holds the agent's identity on disk (/etc/icsd-agent/agent.json).
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strings"
)

const DefaultPath = "/etc/icsd-agent/agent.json"

type Config struct {
	Hub      string `json:"hub"`       // https://panel.example.com
	ServerID string `json:"server_id"` // assigned by the hub at enrollment
	Secret   string `json:"secret"`    // long random; the hub stores only its hash
	Interval int    `json:"interval"`  // seconds between samples
	Docker   string `json:"docker"`    // docker socket path; "off" disables
}

func Load(path string) (*Config, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var c Config
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if c.Hub == "" || c.ServerID == "" || c.Secret == "" {
		return nil, errors.New(path + ": not enrolled (run: icsd-agent enroll)")
	}
	if c.Interval < 2 {
		c.Interval = 5
	}
	return &c, nil
}

// Save writes atomically with 0600: the secret must never be world-readable.
func (c *Config) Save(path string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		return err
	}
	b, _ := json.MarshalIndent(c, "", "  ")
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// NormalizeHub validates the hub URL. Plain http is allowed only for
// localhost and private addresses (tests, LAN); anything else must be https.
func NormalizeHub(raw string) (string, error) {
	u, err := url.Parse(strings.TrimRight(strings.TrimSpace(raw), "/"))
	if err != nil || u.Host == "" {
		return "", fmt.Errorf("invalid hub URL %q", raw)
	}
	switch u.Scheme {
	case "https":
	case "http":
		h := u.Hostname()
		ip := net.ParseIP(h)
		if h != "localhost" && (ip == nil || !(ip.IsLoopback() || ip.IsPrivate())) {
			return "", fmt.Errorf("hub must use https (got %s)", raw)
		}
	default:
		return "", fmt.Errorf("hub URL must start with https:// (got %s)", raw)
	}
	u.Path, u.RawQuery, u.Fragment = "", "", ""
	return u.String(), nil
}
