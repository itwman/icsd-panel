// icsd-agent — the small program that runs on every managed server.
//
//	icsd-agent enroll --hub https://panel.example.com --token XXXX
//	icsd-agent run
//	icsd-agent collect      # print one sample as JSON (debugging)
//	icsd-agent version
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/itwman/icsd-panel/agent/internal/collect"
	"github.com/itwman/icsd-panel/agent/internal/config"
	"github.com/itwman/icsd-panel/agent/internal/link"
)

// Version is stamped at build time: -ldflags "-X main.Version=2.0.0".
var Version = "dev"

func usage() {
	fmt.Fprintf(os.Stderr, `icsd-agent %s

Usage:
  icsd-agent enroll --hub URL --token TOKEN [--config PATH]
  icsd-agent run [--config PATH]
  icsd-agent collect
  icsd-agent version
`, Version)
	os.Exit(2)
}

func main() {
	log.SetFlags(0) // journald adds timestamps
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "enroll":
		enroll(os.Args[2:])
	case "run":
		run(os.Args[2:])
	case "collect":
		collectOnce()
	case "version", "--version", "-v":
		fmt.Println(Version)
	default:
		usage()
	}
}

func collector(cfg *config.Config) *collect.Collector {
	c := collect.New()
	if cfg != nil && cfg.Docker != "" {
		if cfg.Docker == "off" {
			c.DockerSock = ""
		} else {
			c.DockerSock = cfg.Docker
		}
	}
	return c
}

func enroll(args []string) {
	fs := flag.NewFlagSet("enroll", flag.ExitOnError)
	hub := fs.String("hub", "", "hub URL, e.g. https://panel.example.com")
	token := fs.String("token", "", "one-time enrollment token from the panel")
	path := fs.String("config", config.DefaultPath, "where to store the identity")
	docker := fs.String("docker", "", `docker socket path, or "off"`)
	_ = fs.Parse(args)
	if *hub == "" || *token == "" {
		log.Fatal("enroll: --hub and --token are required")
	}
	u, err := config.NormalizeHub(*hub)
	if err != nil {
		log.Fatal(err)
	}
	cfg, err := link.Enroll(u, *token, Version, collector(nil).Host())
	if err != nil {
		log.Fatal(err)
	}
	cfg.Docker = *docker
	if err := cfg.Save(*path); err != nil {
		log.Fatalf("save %s: %v", *path, err)
	}
	fmt.Printf("enrolled as %s — identity saved to %s\n", cfg.ServerID, *path)
}

func run(args []string) {
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	path := fs.String("config", config.DefaultPath, "identity file")
	_ = fs.Parse(args)
	cfg, err := config.Load(*path)
	if err != nil {
		log.Fatal(err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	log.Printf("icsd-agent %s → %s (server %s, every %ds)", Version, cfg.Hub, cfg.ServerID, cfg.Interval)
	link.New(cfg, collector(cfg), Version).Run(ctx)
}

func collectOnce() {
	c := collector(nil)
	c.SlowEvery = 1
	c.Prime()
	time.Sleep(time.Second)
	out := map[string]any{"host": c.Host(), "sample": c.Sample()}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	_ = enc.Encode(out)
}
