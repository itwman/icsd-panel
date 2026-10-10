package actions

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// Backups are files in policy.backup_dir (default /var/backups/icsd):
//
//	<prefix>-<YYYYmmdd-HHMMSS>.<ext>       made on this server
//	remote/<source>/<prefix>-….<ext>        copies received from other servers
//
// The hub schedules them, copies them between servers and keeps the last N.

var prefixRe = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,40}$`)

func (r *Runner) backupRun(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Kind    string   `json:"kind"` // db | files
		Prefix  string   `json:"prefix"`
		DB      DB       `json:"db"`
		Path    string   `json:"path"`
		Exclude []string `json:"exclude"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	if !prefixRe.MatchString(in.Prefix) {
		return nil, errors.New("پیشوند نامعتبر")
	}
	dir, err := r.ensureBackupDir("")
	if err != nil {
		return nil, err
	}
	switch in.Kind {
	case "db":
		c, err := r.target(context.Background(), in.DB)
		if err != nil {
			return nil, err
		}
		file := filepath.Join(dir, fmt.Sprintf("%s-%s%s", in.Prefix, stamp(), dumpExt(in.DB.Engine)))
		return r.Start("backup", in.DB.Name, func(ctx context.Context, j *Job) (any, error) {
			if err := guard(dir, in.DB.Size/3); err != nil {
				return nil, err
			}
			size, err := dumpTo(ctx, in.DB, c, file)
			if err != nil {
				return nil, err
			}
			j.Logf("dump: %s", human(size))
			return map[string]any{"path": file, "size": size}, nil
		}), nil
	case "files":
		src, err := r.Policy.Check(in.Path, false)
		if err != nil {
			return nil, err
		}
		var excl []string
		for _, e := range in.Exclude {
			if e = strings.TrimSpace(e); e == "" {
				continue
			}
			if !filepath.IsAbs(e) {
				e = filepath.Join(src, e)
			}
			excl = append(excl, filepath.Clean(e))
		}
		// Never back up the backups.
		if bd, err := resolve(r.Policy.BackupDir); err == nil {
			excl = append(excl, bd)
		}
		file := filepath.Join(dir, fmt.Sprintf("%s-%s.tar.gz", in.Prefix, stamp()))
		return r.Start("backup", filepath.Base(src), func(ctx context.Context, j *Job) (any, error) {
			need := dirSize(src, excl) / 2 // media compresses little, text a lot: a rough half
			if err := guard(dir, need); err != nil {
				return nil, err
			}
			size, err := writeTarGz(ctx, file, []string{src}, excl, j)
			if err != nil {
				os.Remove(file)
				return nil, err
			}
			return map[string]any{"path": file, "size": size}, nil
		}), nil
	}
	return nil, errors.New("نوع بک‌آپ نامعتبر")
}

func dirSize(root string, excl []string) int64 {
	var total int64
	_ = filepath.Walk(root, func(p string, fi os.FileInfo, err error) error {
		if err != nil {
			return nil
		}
		for _, e := range excl {
			if within(p, e) {
				if fi.IsDir() {
					return filepath.SkipDir
				}
				return nil
			}
		}
		if fi.Mode().IsRegular() {
			total += fi.Size()
		}
		return nil
	})
	return total
}

type BackupFile struct {
	Path  string `json:"path"`
	Name  string `json:"name"`
	Size  int64  `json:"size"`
	Mtime int64  `json:"mtime"`
	From  string `json:"from,omitempty"` // remote/<from>
}

func (r *Runner) backupList(_ context.Context, _ json.RawMessage) (any, error) {
	root, err := r.ensureBackupDir("")
	if err != nil {
		return nil, err
	}
	var out []BackupFile
	stale := time.Now().Add(-24 * time.Hour)
	_ = filepath.Walk(root, func(p string, fi os.FileInfo, err error) error {
		if err != nil || !fi.Mode().IsRegular() {
			return nil
		}
		if strings.HasPrefix(fi.Name(), ".icsd-upload-") {
			if fi.ModTime().Before(stale) { // an interrupted copy: reclaim the space
				os.Remove(p)
			}
			return nil
		}
		rel, _ := filepath.Rel(root, p)
		bf := BackupFile{Path: p, Name: fi.Name(), Size: fi.Size(), Mtime: fi.ModTime().Unix()}
		parts := strings.Split(rel, string(filepath.Separator))
		if len(parts) == 3 && parts[0] == "remote" {
			bf.From = parts[1]
		} else if len(parts) == 2 && parts[0] == "pre-restore" {
			bf.From = "pre-restore"
		}
		out = append(out, bf)
		return nil
	})
	sort.Slice(out, func(a, b int) bool { return out[a].Mtime > out[b].Mtime })
	return map[string]any{"dir": root, "files": out, "free": freeBytes(root)}, nil
}

// backupPrune keeps the newest `keep` files of one prefix in one directory
// (inside the backup dir only).
func (r *Runner) backupPrune(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Dir    string `json:"dir"` // "" or "remote/<name>"
		Prefix string `json:"prefix"`
		Keep   int    `json:"keep"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	if !prefixRe.MatchString(in.Prefix) || in.Keep < 1 {
		return nil, errors.New("ورودی نامعتبر")
	}
	root, _ := resolve(r.Policy.BackupDir)
	dir := filepath.Clean(filepath.Join(root, in.Dir))
	if !within(dir, root) {
		return nil, errDenied
	}
	items, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return map[string]any{"deleted": []string{}}, nil
		}
		return nil, err
	}
	var names []string
	for _, it := range items {
		if it.Type().IsRegular() && strings.HasPrefix(it.Name(), in.Prefix+"-") {
			names = append(names, it.Name())
		}
	}
	sort.Strings(names) // the timestamp in the name sorts chronologically
	var deleted []string
	for len(names) > in.Keep {
		p := filepath.Join(dir, names[0])
		if err := os.Remove(p); err == nil {
			deleted = append(deleted, p)
		}
		names = names[1:]
	}
	return map[string]any{"deleted": deleted}, nil
}
