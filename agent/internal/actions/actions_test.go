package actions

import (
	"archive/zip"
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testRunner(t *testing.T) (*Runner, string) {
	t.Helper()
	root := t.TempDir()
	www := filepath.Join(root, "www")
	logs := filepath.Join(root, "log")
	for _, d := range []string{www, logs, filepath.Join(root, "secret")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	p := &Policy{}
	p.Files.Roots = []string{www}
	p.Files.ReadOnly = []string{logs}
	p.BackupDir = filepath.Join(root, "backups")
	p.Services = []string{"*"}
	p.normalize()
	return &Runner{Policy: p, jobs: map[string]*Job{}}, root
}

func call(t *testing.T, r *Runner, op string, args any) (any, error) {
	t.Helper()
	b, _ := json.Marshal(args)
	return r.Handle(context.Background(), op, b)
}

func TestNilRunnerRefusesEverything(t *testing.T) {
	var r *Runner
	if _, err := r.Handle(context.Background(), "fs.list", nil); err == nil {
		t.Fatal("monitor-only agent must refuse")
	}
	if r.Caps()["mode"] != "monitor" {
		t.Fatal("caps")
	}
}

func TestPolicyBoundaries(t *testing.T) {
	r, root := testRunner(t)
	www := filepath.Join(root, "www")
	ok := []string{www, filepath.Join(www, "a/b.txt"), filepath.Join(root, "backups", "x")}
	for _, p := range ok {
		if _, err := r.Policy.Check(p, true); err != nil {
			t.Errorf("%s should be writable: %v", p, err)
		}
	}
	bad := []string{
		filepath.Join(root, "secret", "x"),   // outside roots
		filepath.Join(www, "..", "secret"),   // traversal
		filepath.Join(root, "log", "syslog"), // read-only root, write
		filepath.Join(www, "home", ".ssh", "authorized_keys"),
		"/etc/icsd-agent/policy.json", "/etc/shadow", "relative/path",
	}
	for _, p := range bad {
		if _, err := r.Policy.Check(p, true); err == nil {
			t.Errorf("%s must be refused for writing", p)
		}
	}
	if _, err := r.Policy.Check(filepath.Join(root, "log", "syslog"), false); err != nil {
		t.Error("read-only roots must be readable")
	}
}

func TestSymlinkEscapeIsRefused(t *testing.T) {
	r, root := testRunner(t)
	link := filepath.Join(root, "www", "escape")
	if err := os.Symlink(filepath.Join(root, "secret"), link); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Policy.Check(filepath.Join(link, "x"), false); err == nil {
		t.Fatal("a link inside a root must not lead outside it")
	}
}

func TestWriteReadKeepsOwnerAndMode(t *testing.T) {
	r, root := testRunner(t)
	f := filepath.Join(root, "www", "index.html")
	if err := os.WriteFile(f, []byte("old"), 0o640); err != nil {
		t.Fatal(err)
	}
	fi, _ := os.Stat(f)
	data := base64.StdEncoding.EncodeToString([]byte("hello "))
	first, err := call(t, r, "fs.write", map[string]any{"path": f, "data": data, "offset": 0, "if_mtime": fi.ModTime().Unix()})
	if err != nil {
		t.Fatal(err)
	}
	up := first.(map[string]any)["upload"].(string)
	data2 := base64.StdEncoding.EncodeToString([]byte("world"))
	if _, err := call(t, r, "fs.write", map[string]any{"path": f, "data": data2, "offset": 6, "final": true, "upload": up}); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(f)
	nfi, _ := os.Stat(f)
	if string(b) != "hello world" || nfi.Mode().Perm() != 0o640 {
		t.Fatalf("%q %v", b, nfi.Mode())
	}
	out, err := call(t, r, "fs.read", map[string]any{"path": f, "offset": 6, "length": 5})
	if err != nil {
		t.Fatal(err)
	}
	m := out.(map[string]any)
	raw, _ := base64.StdEncoding.DecodeString(m["data"].(string))
	if string(raw) != "world" || m["eof"] != true {
		t.Fatalf("%v", m)
	}
}

func TestEditorConflictGuard(t *testing.T) {
	r, root := testRunner(t)
	f := filepath.Join(root, "www", "a.txt")
	_ = os.WriteFile(f, []byte("x"), 0o644)
	_, err := call(t, r, "fs.write", map[string]any{"path": f, "data": "eA==", "final": true, "if_mtime": 12345})
	if err == nil {
		t.Fatal("a file changed on the server since it was opened must not be overwritten")
	}
}

func TestRootsCannotBeDeleted(t *testing.T) {
	r, root := testRunner(t)
	if _, err := call(t, r, "fs.delete", map[string]any{"paths": []string{filepath.Join(root, "www")}}); err == nil {
		t.Fatal("deleting a root must be refused")
	}
	sub := filepath.Join(root, "www", "old")
	_ = os.MkdirAll(filepath.Join(sub, "x"), 0o755)
	if _, err := call(t, r, "fs.delete", map[string]any{"paths": []string{sub}}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(sub); !os.IsNotExist(err) {
		t.Fatal("not deleted")
	}
}

func TestZipSlipIsRefused(t *testing.T) {
	r, root := testRunner(t)
	zp := filepath.Join(root, "www", "evil.zip")
	f, _ := os.Create(zp)
	zw := zip.NewWriter(f)
	w, _ := zw.Create("../../secret/pwned.txt")
	w.Write([]byte("x"))
	zw.Close()
	f.Close()
	out, err := call(t, r, "fs.extract", map[string]any{"path": zp, "dest": filepath.Join(root, "www", "out")})
	if err != nil {
		t.Fatal(err)
	}
	id := out.(map[string]any)["job"].(string)
	j := waitJob(t, r, id)
	if _, err := os.Stat(filepath.Join(root, "secret", "pwned.txt")); err == nil {
		t.Fatal("zip slip wrote outside the destination")
	}
	// The entry is cleaned to out/secret/pwned.txt, which is inside: fine.
	if j.State == "error" && !strings.Contains(j.Error, "ناامن") {
		t.Fatalf("%+v", j)
	}
}

func waitJob(t *testing.T, r *Runner, id string) JobView {
	t.Helper()
	for i := 0; i < 200; i++ {
		out, err := call(t, r, "job.status", map[string]any{"id": id})
		if err != nil {
			t.Fatal(err)
		}
		j := out.(JobView)
		if j.State != "running" {
			return j
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("job did not finish")
	return JobView{}
}

func TestArchiveExtractRoundTrip(t *testing.T) {
	r, root := testRunner(t)
	site := filepath.Join(root, "www", "site")
	_ = os.MkdirAll(filepath.Join(site, "static"), 0o755)
	_ = os.WriteFile(filepath.Join(site, "static", "a.css"), []byte("body{}"), 0o644)
	out, _ := call(t, r, "fs.archive", map[string]any{"paths": []string{site}, "dest": filepath.Join(root, "www", "site-copy")})
	j := waitJob(t, r, out.(map[string]any)["job"].(string))
	if j.State != "done" {
		t.Fatalf("%+v", j)
	}
	arc := j.Result.(map[string]any)["path"].(string)
	out, _ = call(t, r, "fs.extract", map[string]any{"path": arc, "dest": filepath.Join(root, "www", "restored")})
	j = waitJob(t, r, out.(map[string]any)["job"].(string))
	b, err := os.ReadFile(filepath.Join(root, "www", "restored", "site", "static", "a.css"))
	if j.State != "done" || err != nil || string(b) != "body{}" {
		t.Fatalf("%+v %v", j, err)
	}
}

func TestFilesBackupAndPrune(t *testing.T) {
	r, root := testRunner(t)
	media := filepath.Join(root, "www", "media")
	_ = os.MkdirAll(filepath.Join(media, "cache"), 0o755)
	_ = os.WriteFile(filepath.Join(media, "p.jpg"), []byte("img"), 0o644)
	_ = os.WriteFile(filepath.Join(media, "cache", "tmp"), []byte("junk"), 0o644)
	for i := 0; i < 3; i++ {
		out, err := call(t, r, "backup.run", map[string]any{"kind": "files", "prefix": "p7", "path": media, "exclude": []string{"cache"}})
		if err != nil {
			t.Fatal(err)
		}
		if j := waitJob(t, r, out.(map[string]any)["job"].(string)); j.State != "done" {
			t.Fatalf("%+v", j)
		}
		time.Sleep(1100 * time.Millisecond) // distinct timestamps
	}
	out, _ := call(t, r, "backup.prune", map[string]any{"prefix": "p7", "keep": 2})
	if n := len(out.(map[string]any)["deleted"].([]string)); n != 1 {
		t.Fatalf("deleted %d", n)
	}
	lst, _ := call(t, r, "backup.list", nil)
	if n := len(lst.(map[string]any)["files"].([]BackupFile)); n != 2 {
		t.Fatalf("%d files", n)
	}
	if _, err := call(t, r, "backup.prune", map[string]any{"dir": "../../", "prefix": "p7", "keep": 1}); err == nil {
		t.Fatal("prune outside the backup dir must be refused")
	}
}

func TestTail(t *testing.T) {
	r, root := testRunner(t)
	f := filepath.Join(root, "log", "app.log")
	var sb strings.Builder
	for i := 0; i < 1000; i++ {
		sb.WriteString("line ")
		sb.WriteString(strings.Repeat("x", i%7))
		sb.WriteString("\n")
	}
	_ = os.WriteFile(f, []byte(sb.String()), 0o644)
	out, err := call(t, r, "fs.tail", map[string]any{"path": f, "lines": 10})
	if err != nil {
		t.Fatal(err)
	}
	if n := len(out.(map[string]any)["lines"].([]string)); n != 10 {
		t.Fatalf("%d", n)
	}
}

func TestServicePolicy(t *testing.T) {
	r, _ := testRunner(t)
	for _, ok := range []string{"nginx", "gunicorn-rajyar.service", "php8.1-fpm"} {
		if !r.Policy.ServiceAllowed(ok) {
			t.Errorf("%s should be allowed", ok)
		}
	}
	for _, no := range []string{"ssh", "sshd.service", "icsd-agent", "systemd-journald", "x y", "../x"} {
		if r.Policy.ServiceAllowed(no) {
			t.Errorf("%s must be denied", no)
		}
	}
}

func TestConsoleIsOffByDefault(t *testing.T) {
	r, _ := testRunner(t)
	if _, err := call(t, r, "exec.run", map[string]any{"cmd": "id"}); err == nil {
		t.Fatal("console must be off unless policy.console is true")
	}
	r.Policy.Console = true
	out, err := call(t, r, "exec.run", map[string]any{"cmd": "echo hi; exit 3"})
	if err != nil {
		t.Fatal(err)
	}
	m := out.(map[string]any)
	if m["output"] != "hi\n" || m["code"] != 3 {
		t.Fatalf("%v", m)
	}
}

func TestDockerDemux(t *testing.T) {
	frame := func(stream byte, s string) []byte {
		b := []byte{stream, 0, 0, 0, 0, 0, 0, byte(len(s))}
		return append(b, s...)
	}
	raw := append(frame(1, "out\n"), frame(2, "err\n")...)
	if got := demux(raw); got != "out\nerr\n" {
		t.Fatalf("%q", got)
	}
	if got := demux([]byte("plain tty\n")); got != "plain tty\n" {
		t.Fatalf("%q", got)
	}
}

func TestNginxSiteParsing(t *testing.T) {
	dir := t.TempDir()
	conf := `server {
    listen 80;
    server_name rajyar.ir www.rajyar.ir;
    location / { proxy_pass http://127.0.0.1:8000; }
}
server {
    listen 443 ssl; # managed by Certbot
    server_name panel.icsd.ir;
    ssl_certificate /etc/letsencrypt/live/panel.icsd.ir/fullchain.pem;
    root /var/www/x;
}`
	p := filepath.Join(dir, "site")
	_ = os.WriteFile(p, []byte(conf), 0o644)
	sites := parseNginxFiles([]string{p})
	if len(sites) != 2 || sites[0].Names[1] != "www.rajyar.ir" || sites[0].ProxyPass != "http://127.0.0.1:8000" {
		t.Fatalf("%+v", sites)
	}
	if !sites[1].SSL || !strings.Contains(sites[1].CertPath, "panel.icsd.ir") || sites[1].Root != "/var/www/x" {
		t.Fatalf("%+v", sites[1])
	}
}

func TestNginxOneLineAndNested(t *testing.T) {
	conf := `server { listen 80; server_name rajyar.ir www.rajyar.ir; root /var/www/rajyar; }
server { listen 8443 ssl; server_name rajyar.ir; ssl_certificate /etc/letsencrypt/live/rajyar.ir/cert.pem; }
http { server { server_name inner.ir; location /api { proxy_pass http://127.0.0.1:9000; } } }`
	sites := parseNginxConf(conf)
	if len(sites) != 3 || sites[0].Root != "/var/www/rajyar" || len(sites[0].Names) != 2 {
		t.Fatalf("%+v", sites)
	}
	if !sites[1].SSL || sites[2].Names[0] != "inner.ir" || sites[2].ProxyPass != "http://127.0.0.1:9000" {
		t.Fatalf("%+v", sites)
	}
}

func TestPlantedSymlinkCannotHijackAnUpload(t *testing.T) {
	r, root := testRunner(t)
	victim := filepath.Join(root, "secret", "passwd")
	_ = os.WriteFile(victim, []byte("root:x:0:0"), 0o644)
	dir := filepath.Join(root, "www")
	// An attacker who can write in the site folder plants a link with an
	// upload-looking name and hopes the agent writes through it.
	_ = os.Symlink(victim, filepath.Join(dir, ".icsd-upload-1234"))
	_, err := call(t, r, "fs.write", map[string]any{"path": filepath.Join(dir, "index.php"),
		"data": base64.StdEncoding.EncodeToString([]byte("PWNED")), "offset": 5, "final": true, "upload": ".icsd-upload-1234"})
	if err == nil {
		t.Fatal("a symlinked temp file must be refused")
	}
	b, _ := os.ReadFile(victim)
	if string(b) != "root:x:0:0" {
		t.Fatalf("victim changed: %q", b)
	}
	// And a fresh upload never reuses a predictable name.
	out, _ := call(t, r, "fs.write", map[string]any{"path": filepath.Join(dir, "a.txt"), "data": "eA==", "offset": 0})
	if out.(map[string]any)["upload"] == ".icsd-upload-1234" {
		t.Fatal("predictable temp name")
	}
}

func TestDeletingALinkKeepsItsTarget(t *testing.T) {
	r, root := testRunner(t)
	rel := filepath.Join(root, "www", "releases", "v42")
	_ = os.MkdirAll(rel, 0o755)
	_ = os.WriteFile(filepath.Join(rel, "app.py"), []byte("x"), 0o644)
	link := filepath.Join(root, "www", "current")
	_ = os.Symlink(rel, link)
	if _, err := call(t, r, "fs.delete", map[string]any{"paths": []string{link}}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Lstat(link); !os.IsNotExist(err) {
		t.Fatal("the link should be gone")
	}
	if _, err := os.Stat(filepath.Join(rel, "app.py")); err != nil {
		t.Fatal("the target must survive")
	}
}

func TestArchiveSkipsSSHKeys(t *testing.T) {
	r, root := testRunner(t)
	home := filepath.Join(root, "www", "user")
	_ = os.MkdirAll(filepath.Join(home, ".ssh"), 0o700)
	_ = os.WriteFile(filepath.Join(home, ".ssh", "id_ed25519"), []byte("PRIVATE"), 0o600)
	_ = os.WriteFile(filepath.Join(home, "site.txt"), []byte("ok"), 0o644)
	out, _ := call(t, r, "backup.run", map[string]any{"kind": "files", "prefix": "p9", "path": home})
	j := waitJob(t, r, out.(map[string]any)["job"].(string))
	if j.State != "done" {
		t.Fatalf("%+v", j)
	}
	arc := j.Result.(map[string]any)["path"].(string)
	b, _ := os.ReadFile(arc)
	gz, _ := gzip.NewReader(bytes.NewReader(b))
	raw, _ := io.ReadAll(gz)
	if bytes.Contains(raw, []byte("PRIVATE")) || !bytes.Contains(raw, []byte("site.txt")) {
		t.Fatal("ssh keys must never be archived")
	}
}

func TestHomeDotfilesAreNotWritable(t *testing.T) {
	if !homeDotfile("/home/ali/.bashrc") || homeDotfile("/home/ali/site/.env") || homeDotfile("/var/www/.htaccess") {
		t.Fatal("homeDotfile")
	}
}

func TestDumpForAnotherDatabaseIsRefused(t *testing.T) {
	br := bufio.NewReader(strings.NewReader("-- MySQL dump\nCREATE DATABASE /*!32312 IF NOT EXISTS*/ `blog`;\nUSE `blog`;\n"))
	if got := otherDatabase(br, "shop"); got != "blog" {
		t.Fatalf("%q", got)
	}
	br = bufio.NewReader(strings.NewReader("-- dump of shop\nDROP TABLE IF EXISTS `items`;\n"))
	if got := otherDatabase(br, "shop"); got != "" {
		t.Fatalf("%q", got)
	}
}

func TestRestoreOnlyFromBackupDir(t *testing.T) {
	r, root := testRunner(t)
	f := filepath.Join(root, "www", "evil.sql.gz")
	_ = os.WriteFile(f, []byte("x"), 0o644)
	r.Policy.Databases = true
	_, err := call(t, r, "db.restore", map[string]any{"engine": "mysql", "where": "native", "name": "shop", "file": f})
	if err == nil || !strings.Contains(err.Error(), "بک‌آپ") {
		t.Fatalf("%v", err)
	}
}
