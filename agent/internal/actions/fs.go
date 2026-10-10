package actions

import (
	"archive/tar"
	"archive/zip"
	"bufio"
	"compress/gzip"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/user"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
)

const maxChunk = 512 * 1024

type Entry struct {
	Name  string `json:"name"`
	Type  string `json:"type"` // file | dir | link | other
	Size  int64  `json:"size"`
	Mtime int64  `json:"mtime"`
	Mode  string `json:"mode"`
	Owner string `json:"owner"`
	Link  string `json:"link,omitempty"`
}

var (
	ownersMu sync.Mutex
	owners   = map[uint32]string{}
)

func ownerOf(fi fs.FileInfo) string {
	st, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		return ""
	}
	ownersMu.Lock()
	defer ownersMu.Unlock()
	if n, ok := owners[st.Uid]; ok {
		return n
	}
	n := strconv.Itoa(int(st.Uid))
	if u, err := user.LookupId(n); err == nil {
		n = u.Username
	}
	owners[st.Uid] = n
	return n
}

func entryOf(dir string, fi fs.FileInfo) Entry {
	e := Entry{Name: fi.Name(), Size: fi.Size(), Mtime: fi.ModTime().Unix(),
		Mode: fmt.Sprintf("%04o", fi.Mode().Perm()), Owner: ownerOf(fi)}
	switch {
	case fi.Mode()&os.ModeSymlink != 0:
		e.Type = "link"
		e.Link, _ = os.Readlink(filepath.Join(dir, fi.Name()))
		if t, err := os.Stat(filepath.Join(dir, fi.Name())); err == nil && t.IsDir() {
			e.Type = "dirlink"
		}
	case fi.IsDir():
		e.Type = "dir"
	case fi.Mode().IsRegular():
		e.Type = "file"
	default:
		e.Type = "other"
	}
	return e
}

func (r *Runner) fsList(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Path string `json:"path"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	if in.Path == "" || in.Path == "/" {
		// The virtual top level: the allowed roots.
		var out []map[string]any
		for _, root := range append(append(append([]string{}, r.Policy.Files.Roots...), r.Policy.BackupDir), r.Policy.Files.ReadOnly...) {
			fi, err := os.Stat(root)
			if err != nil {
				continue
			}
			out = append(out, map[string]any{"name": root, "type": "dir", "mtime": fi.ModTime().Unix(),
				"readonly": r.readOnly(root)})
		}
		return map[string]any{"path": "/", "entries": out, "virtual": true}, nil
	}
	real, err := r.Policy.Check(in.Path, false)
	if err != nil {
		return nil, err
	}
	items, err := os.ReadDir(real)
	if err != nil {
		return nil, err
	}
	out := make([]Entry, 0, len(items))
	for _, it := range items {
		fi, err := it.Info()
		if err != nil {
			continue
		}
		out = append(out, entryOf(real, fi))
		if len(out) >= 5000 {
			break
		}
	}
	sort.Slice(out, func(a, b int) bool {
		da, db := strings.HasSuffix(out[a].Type, "dir"), strings.HasSuffix(out[b].Type, "dir")
		if da != db {
			return da
		}
		return strings.ToLower(out[a].Name) < strings.ToLower(out[b].Name)
	})
	return map[string]any{"path": real, "entries": out, "readonly": r.readOnly(real), "truncated": len(items) > len(out)}, nil
}

func (r *Runner) inBackupDir(real string) bool {
	bd, err := resolve(r.Policy.BackupDir)
	return err == nil && within(real, bd)
}

func (r *Runner) readOnly(real string) bool {
	_, err := r.Policy.Check(real, true)
	return err != nil
}

func (r *Runner) fsStat(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Path string `json:"path"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	real, err := r.Policy.Check(in.Path, false)
	if err != nil {
		return nil, err
	}
	fi, err := os.Lstat(real)
	if err != nil {
		return nil, err
	}
	e := entryOf(filepath.Dir(real), fi)
	return map[string]any{"path": real, "entry": e, "readonly": r.readOnly(real)}, nil
}

func (r *Runner) fsRead(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Path   string `json:"path"`
		Offset int64  `json:"offset"`
		Length int    `json:"length"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	real, err := r.Policy.Check(in.Path, false)
	if err != nil {
		return nil, err
	}
	f, err := os.Open(real)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !fi.Mode().IsRegular() {
		return nil, errors.New("فقط فایل معمولی خوانده می‌شود")
	}
	if in.Length <= 0 || in.Length > maxChunk {
		in.Length = maxChunk
	}
	buf := make([]byte, in.Length)
	n, err := f.ReadAt(buf, in.Offset)
	if err != nil && err != io.EOF {
		return nil, err
	}
	return map[string]any{"data": base64.StdEncoding.EncodeToString(buf[:n]), "size": fi.Size(),
		"eof": in.Offset+int64(n) >= fi.Size(), "mtime": fi.ModTime().Unix()}, nil
}

// fsWrite writes a chunk. The first chunk creates a temporary file with a
// random name (O_EXCL) next to the target and returns its name ("upload");
// later chunks must quote it. The last chunk (final=true) fixes owner and
// mode on the open descriptor and renames it over the target, keeping the
// target's owner/mode (or the directory's owner for a new file), so a site
// owned by www-data keeps working after an edit. Nothing is ever opened
// through a path another user could have planted a symlink at.
func (r *Runner) fsWrite(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Path   string `json:"path"`
		Data   string `json:"data"`
		Offset int64  `json:"offset"`
		Final  bool   `json:"final"`
		Upload string `json:"upload"`
		Mtime  int64  `json:"if_mtime"` // optional conflict guard for the editor
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	real, err := r.Policy.Check(in.Path, true)
	if err != nil {
		return nil, err
	}
	data, err := base64.StdEncoding.DecodeString(in.Data)
	if err != nil {
		return nil, errors.New("دادهٔ نامعتبر")
	}
	if len(data) > maxChunk*2 {
		return nil, errors.New("تکهٔ بزرگ‌تر از حد")
	}
	backup := r.inBackupDir(real)
	if !backup && in.Offset+int64(len(data)) > int64(r.Policy.Files.MaxMB)<<20 {
		return nil, fmt.Errorf("بزرگ‌تر از سقف %d مگابایت", r.Policy.Files.MaxMB)
	}
	old, statErr := os.Stat(real)
	if statErr == nil && old.IsDir() {
		return nil, errors.New("این مسیر یک پوشه است")
	}
	if in.Offset == 0 && in.Mtime != 0 && statErr == nil && old.ModTime().Unix() != in.Mtime {
		return nil, errors.New("فایل در این فاصله روی سرور تغییر کرده است؛ دوباره باز کنید")
	}
	dir := filepath.Dir(real)
	var f *os.File
	if in.Offset == 0 {
		f, err = os.CreateTemp(dir, ".icsd-upload-*")
	} else {
		f, err = openUpload(dir, in.Upload)
	}
	if err != nil {
		return nil, err
	}
	if _, err := f.WriteAt(data, in.Offset); err != nil {
		f.Close()
		os.Remove(f.Name())
		return nil, err
	}
	if !in.Final {
		name := filepath.Base(f.Name())
		if err := f.Close(); err != nil {
			return nil, err
		}
		return map[string]any{"written": in.Offset + int64(len(data)), "upload": name}, nil
	}
	mode, uid, gid := fs.FileMode(0o644), -1, -1
	if backup {
		mode, uid, gid = 0o600, 0, 0 // database dumps: root only
	} else if statErr == nil {
		mode = old.Mode().Perm()
		if st, ok := old.Sys().(*syscall.Stat_t); ok {
			uid, gid = int(st.Uid), int(st.Gid)
		}
	} else if dfi, err := os.Stat(dir); err == nil {
		if st, ok := dfi.Sys().(*syscall.Stat_t); ok {
			uid, gid = int(st.Uid), int(st.Gid)
		}
	}
	_ = f.Chmod(mode)
	_ = f.Chown(uid, gid)
	tmp := f.Name()
	if err := f.Close(); err != nil {
		os.Remove(tmp)
		return nil, err
	}
	if err := os.Rename(tmp, real); err != nil {
		os.Remove(tmp)
		return nil, err
	}
	fi, _ := os.Stat(real)
	return map[string]any{"path": real, "size": fi.Size(), "mtime": fi.ModTime().Unix()}, nil
}

var uploadName = regexp.MustCompile(`^\.icsd-upload-[0-9]+$`)

// openUpload reopens a temporary file this agent created: never through a
// symlink, and only if it is still a root-owned regular file.
func openUpload(dir, name string) (*os.File, error) {
	if !uploadName.MatchString(name) {
		return nil, errors.New("شناسهٔ آپلود نامعتبر؛ از اول بفرستید")
	}
	f, err := os.OpenFile(filepath.Join(dir, name), os.O_WRONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, errors.New("آپلود ناتمام پیدا نشد؛ از اول بفرستید")
	}
	fi, err := f.Stat()
	st, ok := fi.Sys().(*syscall.Stat_t)
	if err != nil || !fi.Mode().IsRegular() || !ok || st.Uid != 0 {
		f.Close()
		return nil, errors.New("فایل موقت آپلود دست‌کاری شده است")
	}
	return f, nil
}

// fsAbort removes an unfinished upload (after a failed copy).
func (r *Runner) fsAbort(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Path   string `json:"path"`
		Upload string `json:"upload"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	real, err := r.Policy.Check(in.Path, true)
	if err != nil {
		return nil, err
	}
	f, err := openUpload(filepath.Dir(real), in.Upload)
	if err != nil {
		return map[string]any{"removed": false}, nil
	}
	f.Close()
	return map[string]any{"removed": os.Remove(filepath.Join(filepath.Dir(real), in.Upload)) == nil}, nil
}

func chownLikeParent(p string) {
	if dfi, err := os.Stat(filepath.Dir(p)); err == nil {
		if st, ok := dfi.Sys().(*syscall.Stat_t); ok {
			_ = os.Lchown(p, int(st.Uid), int(st.Gid))
		}
	}
}

func (r *Runner) fsMkdir(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Path    string `json:"path"`
		Parents bool   `json:"parents"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	real, err := r.Policy.Check(in.Path, true)
	if err != nil {
		return nil, err
	}
	if in.Parents {
		perm := fs.FileMode(0o755)
		if r.inBackupDir(real) {
			perm = 0o700
		}
		if err := os.MkdirAll(real, perm); err != nil {
			return nil, err
		}
		return map[string]any{"path": real}, nil
	}
	if err := os.Mkdir(real, 0o755); err != nil {
		return nil, err
	}
	chownLikeParent(real)
	return map[string]any{"path": real}, nil
}

func (r *Runner) fsRename(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		From string `json:"from"`
		To   string `json:"to"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	src, err := r.Policy.CheckEntry(in.From)
	if err != nil {
		return nil, err
	}
	dst, err := r.Policy.CheckEntry(in.To)
	if err != nil {
		return nil, err
	}
	if r.Policy.IsRoot(src) {
		return nil, errors.New("پوشه‌های اصلی را نمی‌شود جابه‌جا کرد")
	}
	if _, err := os.Lstat(dst); err == nil {
		return nil, errors.New("مقصد از قبل وجود دارد")
	}
	if err := os.Rename(src, dst); err != nil {
		return nil, err
	}
	return map[string]any{"path": dst}, nil
}

func (r *Runner) fsDelete(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Paths []string `json:"paths"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	var done []string
	for _, p := range in.Paths {
		real, err := r.Policy.CheckEntry(p)
		if err != nil {
			return map[string]any{"deleted": done}, fmt.Errorf("%s: %v", p, err)
		}
		if r.Policy.IsRoot(real) {
			return map[string]any{"deleted": done}, errors.New("پوشه‌های اصلی را نمی‌شود حذف کرد")
		}
		if err := os.RemoveAll(real); err != nil {
			return map[string]any{"deleted": done}, err
		}
		done = append(done, real)
	}
	return map[string]any{"deleted": done}, nil
}

func (r *Runner) fsChmod(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Path string `json:"path"`
		Mode string `json:"mode"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	real, err := r.Policy.Check(in.Path, true)
	if err != nil {
		return nil, err
	}
	m, err := strconv.ParseUint(in.Mode, 8, 32)
	if err != nil || m > 0o777 {
		return nil, errors.New("دسترسی نامعتبر (مثل 644 یا 755)")
	}
	if err := os.Chmod(real, fs.FileMode(m)); err != nil {
		return nil, err
	}
	return map[string]any{"path": real, "mode": fmt.Sprintf("%04o", m)}, nil
}

// fsTail returns the last lines of a (log) file without reading it whole.
func (r *Runner) fsTail(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Path  string `json:"path"`
		Lines int    `json:"lines"`
		Grep  string `json:"grep"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	real, err := r.Policy.Check(in.Path, false)
	if err != nil {
		return nil, err
	}
	if in.Lines <= 0 || in.Lines > 5000 {
		in.Lines = 300
	}
	return tailFile(real, in.Lines, in.Grep)
}

func tailFile(p string, lines int, grep string) (any, error) {
	f, err := os.Open(p)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	fi, _ := f.Stat()
	const window = 4 << 20
	start := int64(0)
	if fi.Size() > window {
		start = fi.Size() - window
	}
	if _, err := f.Seek(start, io.SeekStart); err != nil {
		return nil, err
	}
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	ring := make([]string, 0, lines)
	first := start > 0
	for sc.Scan() {
		if first { // a partial line
			first = false
			continue
		}
		t := sc.Text()
		if grep != "" && !strings.Contains(t, grep) {
			continue
		}
		if len(ring) == lines {
			ring = ring[1:]
		}
		ring = append(ring, t)
	}
	return map[string]any{"lines": ring, "size": fi.Size()}, nil
}

// ── archives (background jobs) ──────────────────────────────────────

func (r *Runner) fsArchive(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Paths []string `json:"paths"`
		Dest  string   `json:"dest"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	if len(in.Paths) == 0 {
		return nil, errors.New("چیزی انتخاب نشده")
	}
	var srcs []string
	for _, p := range in.Paths {
		real, err := r.Policy.Check(p, false)
		if err != nil {
			return nil, err
		}
		srcs = append(srcs, real)
	}
	dest, err := r.Policy.Check(in.Dest, true)
	if err != nil {
		return nil, err
	}
	if !strings.HasSuffix(dest, ".tar.gz") {
		dest += ".tar.gz"
	}
	return r.Start("archive", filepath.Base(dest), func(ctx context.Context, j *Job) (any, error) {
		size, err := writeTarGz(ctx, dest, srcs, nil, j)
		if err != nil {
			os.Remove(dest)
			return nil, err
		}
		chownLikeParent(dest)
		return map[string]any{"path": dest, "size": size}, nil
	}), nil
}

// writeTarGz archives srcs (paths stored relative to their parent dir).
func writeTarGz(ctx context.Context, dest string, srcs []string, exclude []string, j *Job) (int64, error) {
	f, err := os.OpenFile(dest, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return 0, err
	}
	gz, _ := gzip.NewWriterLevel(f, gzip.BestSpeed)
	tw := tar.NewWriter(gz)
	files := 0
	for _, src := range srcs {
		base := filepath.Dir(src)
		err := filepath.Walk(src, func(p string, fi os.FileInfo, err error) error {
			if err != nil {
				return nil // unreadable entry: skip, keep going
			}
			if ctx.Err() != nil {
				return ctx.Err()
			}
			if deniedLexical(p) {
				if fi.IsDir() {
					return filepath.SkipDir
				}
				return nil
			}
			for _, ex := range exclude {
				if within(p, ex) {
					if fi.IsDir() {
						return filepath.SkipDir
					}
					return nil
				}
			}
			if p == dest {
				return nil
			}
			link := ""
			if fi.Mode()&os.ModeSymlink != 0 {
				link, _ = os.Readlink(p)
			}
			hdr, err := tar.FileInfoHeader(fi, link)
			if err != nil {
				return nil
			}
			rel, _ := filepath.Rel(base, p)
			hdr.Name = filepath.ToSlash(rel)
			if fi.IsDir() {
				hdr.Name += "/"
			}
			if err := tw.WriteHeader(hdr); err != nil {
				return err
			}
			if fi.Mode().IsRegular() {
				in, err := os.Open(p)
				if err != nil {
					return nil
				}
				_, err = io.Copy(tw, in)
				in.Close()
				if err != nil {
					return err
				}
				files++
				if j != nil && files%2000 == 0 {
					j.Logf("%d فایل…", files)
				}
			}
			return nil
		})
		if err != nil {
			tw.Close()
			gz.Close()
			f.Close()
			return 0, err
		}
	}
	if err := tw.Close(); err != nil {
		return 0, err
	}
	if err := gz.Close(); err != nil {
		return 0, err
	}
	if err := f.Close(); err != nil {
		return 0, err
	}
	fi, _ := os.Stat(dest)
	if j != nil {
		j.Logf("%d فایل، %d بایت", files, fi.Size())
	}
	return fi.Size(), nil
}

func (r *Runner) fsExtract(_ context.Context, args json.RawMessage) (any, error) {
	var in struct {
		Path string `json:"path"`
		Dest string `json:"dest"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	src, err := r.Policy.Check(in.Path, false)
	if err != nil {
		return nil, err
	}
	dest, err := r.Policy.Check(in.Dest, true)
	if err != nil {
		return nil, err
	}
	return r.Start("extract", filepath.Base(src), func(ctx context.Context, j *Job) (any, error) {
		if err := os.MkdirAll(dest, 0o755); err != nil {
			return nil, err
		}
		var n int
		var err error
		switch {
		case strings.HasSuffix(src, ".zip"):
			n, err = extractZip(ctx, src, dest, r.Policy)
		case strings.HasSuffix(src, ".tar.gz"), strings.HasSuffix(src, ".tgz"):
			n, err = extractTarGz(ctx, src, dest, r.Policy)
		default:
			return nil, errors.New("فقط zip و tar.gz")
		}
		if err != nil {
			return nil, err
		}
		j.Logf("%d فایل باز شد", n)
		return map[string]any{"dest": dest, "files": n}, nil
	}), nil
}

// safeJoin rejects "zip slip" entries (../, absolute paths) and anything the
// policy would not allow writing.
func safeJoin(dest, name string, p *Policy) (string, error) {
	clean := filepath.Clean("/" + name)
	target := filepath.Join(dest, clean)
	if !within(target, dest) {
		return "", fmt.Errorf("مسیر ناامن در آرشیو: %s", name)
	}
	if _, err := p.Check(target, true); err != nil {
		return "", fmt.Errorf("%s: %v", name, err)
	}
	return target, nil
}

func extractZip(ctx context.Context, src, dest string, p *Policy) (int, error) {
	zr, err := zip.OpenReader(src)
	if err != nil {
		return 0, err
	}
	defer zr.Close()
	n := 0
	for _, zf := range zr.File {
		if ctx.Err() != nil {
			return n, ctx.Err()
		}
		target, err := safeJoin(dest, zf.Name, p)
		if err != nil {
			return n, err
		}
		if zf.FileInfo().IsDir() {
			_ = os.MkdirAll(target, 0o755)
			continue
		}
		if zf.Mode()&os.ModeSymlink != 0 {
			continue // links from archives are not followed or created
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return n, err
		}
		rc, err := zf.Open()
		if err != nil {
			return n, err
		}
		err = writeFileFrom(target, rc, zf.Mode().Perm())
		rc.Close()
		if err != nil {
			return n, err
		}
		n++
	}
	return n, nil
}

func extractTarGz(ctx context.Context, src, dest string, p *Policy) (int, error) {
	f, err := os.Open(src)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return 0, err
	}
	tr := tar.NewReader(gz)
	n := 0
	for {
		if ctx.Err() != nil {
			return n, ctx.Err()
		}
		hdr, err := tr.Next()
		if err == io.EOF {
			return n, nil
		}
		if err != nil {
			return n, err
		}
		target, err := safeJoin(dest, hdr.Name, p)
		if err != nil {
			return n, err
		}
		switch hdr.Typeflag {
		case tar.TypeDir:
			_ = os.MkdirAll(target, 0o755)
		case tar.TypeReg:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return n, err
			}
			if err := writeFileFrom(target, tr, fs.FileMode(hdr.Mode).Perm()); err != nil {
				return n, err
			}
			n++
		}
	}
}

func writeFileFrom(target string, r io.Reader, mode fs.FileMode) error {
	if fi, err := os.Lstat(target); err == nil && fi.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("%s یک پیوند است؛ بازنویسی نمی‌شود", target)
	}
	if mode == 0 {
		mode = 0o644
	}
	f, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, mode)
	if err != nil {
		return err
	}
	if _, err := io.Copy(f, io.LimitReader(r, 10<<30)); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	chownLikeParent(target)
	return nil
}
