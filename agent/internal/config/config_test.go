package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestHubMustBeHTTPSExceptPrivate(t *testing.T) {
	ok := map[string]string{
		"https://panel.example.com/":      "https://panel.example.com",
		"https://panel.example.com/x?y=1": "https://panel.example.com",
		"http://127.0.0.1:8088":           "http://127.0.0.1:8088",
		"http://10.0.0.5:8088":            "http://10.0.0.5:8088",
		"http://localhost:8088":           "http://localhost:8088",
	}
	for in, want := range ok {
		got, err := NormalizeHub(in)
		if err != nil || got != want {
			t.Errorf("%s → %q, %v", in, got, err)
		}
	}
	for _, bad := range []string{"http://panel.example.com", "http://8.8.8.8", "ftp://x", "panel.example.com", ""} {
		if _, err := NormalizeHub(bad); err == nil {
			t.Errorf("%s must be rejected", bad)
		}
	}
}

func TestSaveIsPrivate(t *testing.T) {
	p := filepath.Join(t.TempDir(), "sub", "agent.json")
	c := &Config{Hub: "https://h", ServerID: "s1", Secret: "x", Interval: 5}
	if err := c.Save(p); err != nil {
		t.Fatal(err)
	}
	st, _ := os.Stat(p)
	if st.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v: the secret must not be readable by others", st.Mode().Perm())
	}
	got, err := Load(p)
	if err != nil || *got != *c {
		t.Fatalf("%+v %v", got, err)
	}
}

func TestLoadRejectsUnenrolled(t *testing.T) {
	p := filepath.Join(t.TempDir(), "a.json")
	_ = os.WriteFile(p, []byte(`{"hub":"https://h"}`), 0o600)
	if _, err := Load(p); err == nil {
		t.Fatal("expected error")
	}
}
