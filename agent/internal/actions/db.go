package actions

import (
	"bufio"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// Databases: MySQL/MariaDB and PostgreSQL, installed on the host or running
// in Docker containers. Everything goes through the engines' own client
// tools; passwords are never handled by the agent (root uses socket auth on
// the host; inside containers the image's own environment variable is used
// by a shell *inside* the container).

type DB struct {
	Engine string `json:"engine"` // mysql | postgres
	Where  string `json:"where"`  // native | docker:<container>
	Name   string `json:"name"`
	Size   int64  `json:"size"`
}

var (
	dbName  = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_$.-]{0,63}$`)
	pgUser  = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$`)
	sysDBs  = map[string]bool{"information_schema": true, "performance_schema": true, "mysql": true, "sys": true}
	mysqlPw = `${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD:-}}`
)

const mysqlSizes = `SELECT s.schema_name, COALESCE(SUM(t.data_length+t.index_length),0) ` +
	`FROM information_schema.schemata s LEFT JOIN information_schema.tables t ` +
	`ON t.table_schema=s.schema_name GROUP BY s.schema_name`
const pgSizes = `select datname, pg_database_size(datname) from pg_database where not datistemplate`

func has(bin string) bool { _, err := exec.LookPath(bin); return err == nil }

type dockerDB struct{ name, engine, user string }

func (r *Runner) dockerDBs(ctx context.Context) []dockerDB {
	c, err := r.docker()
	if err != nil || !has("docker") {
		return nil
	}
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, "http://docker/containers/json", nil)
	resp, err := c.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	var items []struct {
		ID    string   `json:"Id"`
		Names []string `json:"Names"`
		Image string   `json:"Image"`
	}
	if json.NewDecoder(resp.Body).Decode(&items) != nil {
		return nil
	}
	var out []dockerDB
	for _, it := range items {
		img := strings.ToLower(it.Image)
		engine := ""
		switch {
		case strings.Contains(img, "postgres") || strings.Contains(img, "postgis"):
			engine = "postgres"
		case strings.Contains(img, "mysql") || strings.Contains(img, "mariadb"):
			engine = "mysql"
		default:
			continue
		}
		name := strings.TrimPrefix(firstOr(it.Names, it.ID), "/")
		d := dockerDB{name: name, engine: engine, user: "postgres"}
		if engine == "postgres" {
			if env, err := run(ctx, 10*time.Second, "docker", "inspect", "-f", `{{range .Config.Env}}{{println .}}{{end}}`, name); err == nil {
				for _, l := range strings.Split(env, "\n") {
					if v, ok := strings.CutPrefix(l, "POSTGRES_USER="); ok && pgUser.MatchString(v) {
						d.user = v
					}
				}
			}
		}
		out = append(out, d)
	}
	return out
}

func firstOr(s []string, d string) string {
	if len(s) > 0 {
		return s[0]
	}
	return d
}

func parseSizes(out, sep string) map[string]int64 {
	m := map[string]int64{}
	for _, line := range strings.Split(out, "\n") {
		f := strings.Split(strings.TrimSpace(line), sep)
		if len(f) == 2 {
			n, _ := strconv.ParseInt(strings.TrimSpace(f[1]), 10, 64)
			m[strings.TrimSpace(f[0])] = n
		}
	}
	return m
}

func (r *Runner) dbList(ctx context.Context, _ json.RawMessage) (any, error) {
	if !r.Policy.Databases {
		return nil, errors.New("مدیریت دیتابیس در policy این سرور خاموش است")
	}
	var dbs []DB
	var notes []string
	if has("mysql") {
		if out, err := run(ctx, 20*time.Second, "mysql", "--batch", "--skip-column-names", "-e", mysqlSizes); err == nil {
			for n, s := range parseSizes(out, "\t") {
				if !sysDBs[n] {
					dbs = append(dbs, DB{"mysql", "native", n, s})
				}
			}
		} else {
			notes = append(notes, "MySQL: "+err.Error())
		}
	}
	if has("psql") && has("runuser") {
		if out, err := run(ctx, 20*time.Second, "runuser", "-u", "postgres", "--", "psql", "-XAt", "-F", "|", "-c", pgSizes); err == nil {
			for n, s := range parseSizes(out, "|") {
				if n != "postgres" {
					dbs = append(dbs, DB{"postgres", "native", n, s})
				}
			}
		} else {
			notes = append(notes, "PostgreSQL: "+err.Error())
		}
	}
	for _, d := range r.dockerDBs(ctx) {
		var out string
		var err error
		if d.engine == "postgres" {
			out, err = run(ctx, 20*time.Second, "docker", "exec", d.name, "psql", "-U", d.user, "-d", "postgres", "-XAt", "-F", "|", "-c", pgSizes)
		} else {
			out, err = run(ctx, 20*time.Second, "docker", "exec", d.name, "sh", "-c",
				`exec mysql -uroot -p"`+mysqlPw+`" --batch --skip-column-names -e "$0"`, mysqlSizes)
		}
		if err != nil {
			notes = append(notes, d.name+": "+err.Error())
			continue
		}
		sep := "|"
		if d.engine == "mysql" {
			sep = "\t"
		}
		for n, s := range parseSizes(out, sep) {
			if !sysDBs[n] && n != "postgres" {
				dbs = append(dbs, DB{d.engine, "docker:" + d.name, n, s})
			}
		}
	}
	return map[string]any{"databases": dbs, "notes": notes}, nil
}

// target validates a database reference coming from the hub.
func (r *Runner) target(ctx context.Context, d DB) (dockerDB, error) {
	if !r.Policy.Databases {
		return dockerDB{}, errors.New("مدیریت دیتابیس در policy این سرور خاموش است")
	}
	if !dbName.MatchString(d.Name) || (d.Engine != "mysql" && d.Engine != "postgres") {
		return dockerDB{}, errors.New("نام دیتابیس نامعتبر")
	}
	if d.Where == "native" {
		return dockerDB{engine: d.Engine}, nil
	}
	name, ok := strings.CutPrefix(d.Where, "docker:")
	if !ok || !containerRef.MatchString(name) {
		return dockerDB{}, errors.New("محل دیتابیس نامعتبر")
	}
	for _, c := range r.dockerDBs(ctx) {
		if c.name == name && c.engine == d.Engine {
			return c, nil
		}
	}
	return dockerDB{}, errors.New("کانتینر دیتابیس پیدا نشد")
}

func dumpExt(engine string) string {
	if engine == "postgres" {
		return ".dump"
	}
	return ".sql.gz"
}

// dumpCmd builds the command whose stdout is the dump.
func dumpCmd(ctx context.Context, d DB, c dockerDB) *exec.Cmd {
	switch {
	case d.Where == "native" && d.Engine == "mysql":
		return exec.CommandContext(ctx, "mysqldump", "--single-transaction", "--quick", "--routines",
			"--triggers", "--events", d.Name)
	case d.Where == "native":
		return exec.CommandContext(ctx, "runuser", "-u", "postgres", "--", "pg_dump", "-Fc", d.Name)
	case d.Engine == "postgres":
		return exec.CommandContext(ctx, "docker", "exec", c.name, "pg_dump", "-U", c.user, "-Fc", d.Name)
	default:
		return exec.CommandContext(ctx, "docker", "exec", c.name, "sh", "-c",
			`exec mysqldump -uroot -p"`+mysqlPw+`" --single-transaction --quick --routines --triggers --events "$0"`, d.Name)
	}
}

// dumpTo writes the dump to file (gzip for MySQL; pg custom format is
// already compressed). Partial files are removed on failure.
func dumpTo(ctx context.Context, d DB, c dockerDB, file string) (int64, error) {
	f, err := os.OpenFile(file, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return 0, err
	}
	ok := false
	defer func() {
		if !ok {
			os.Remove(file)
		}
	}()
	cmd := dumpCmd(ctx, d, c)
	var stderr strings.Builder
	cmd.Stderr = &limited{w: &stderr, n: 8192}
	var w io.Writer = f
	var gz *gzip.Writer
	if d.Engine == "mysql" {
		gz = gzip.NewWriter(f)
		w = gz
	}
	bw := bufio.NewWriterSize(w, 1<<20)
	cmd.Stdout = bw
	if err := cmd.Run(); err != nil {
		f.Close()
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		return 0, fmt.Errorf("dump: %s", lastLines(msg, 4))
	}
	if err := bw.Flush(); err != nil {
		f.Close()
		return 0, err
	}
	if gz != nil {
		if err := gz.Close(); err != nil {
			f.Close()
			return 0, err
		}
	}
	if err := f.Close(); err != nil {
		return 0, err
	}
	fi, _ := os.Stat(file)
	if fi.Size() < 20 {
		return 0, errors.New("dump خالی است")
	}
	ok = true
	return fi.Size(), nil
}

func freeBytes(dir string) int64 {
	var st syscall.Statfs_t
	if syscall.Statfs(dir, &st) != nil {
		return -1
	}
	return int64(st.Bavail) * int64(st.Bsize)
}

// guard refuses to start a backup that would fill the disk — the cause of
// the 500 errors that started this whole panel.
func guard(dir string, need int64) error {
	free := freeBytes(dir)
	if free < 0 {
		return nil
	}
	const reserve = 1 << 30
	if free < need+reserve {
		return fmt.Errorf("فضای کافی نیست: %s آزاد، دست‌کم %s لازم است (برآورد + ۱ گیگ ذخیره)",
			human(free), human(need+reserve))
	}
	return nil
}

func human(n int64) string {
	f := float64(n)
	for _, u := range []string{"B", "KB", "MB", "GB", "TB"} {
		if f < 1024 || u == "TB" {
			return fmt.Sprintf("%.1f %s", f, u)
		}
		f /= 1024
	}
	return ""
}

func (r *Runner) ensureBackupDir(sub string) (string, error) {
	dir := filepath.Join(r.Policy.BackupDir, sub)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	return dir, nil
}

func stamp() string { return time.Now().Format("20060102-150405") }

func (r *Runner) dbDump(ctx context.Context, args json.RawMessage) (any, error) {
	var d DB
	if err := decode(args, &d); err != nil {
		return nil, err
	}
	c, err := r.target(ctx, d)
	if err != nil {
		return nil, err
	}
	dir, err := r.ensureBackupDir("")
	if err != nil {
		return nil, err
	}
	file := filepath.Join(dir, fmt.Sprintf("db-%s-%s%s", d.Name, stamp(), dumpExt(d.Engine)))
	return r.Start("db.dump", d.Name, func(ctx context.Context, j *Job) (any, error) {
		if err := guard(dir, d.Size/3); err != nil {
			return nil, err
		}
		j.Logf("dump %s (%s) …", d.Name, d.Where)
		size, err := dumpTo(ctx, d, c, file)
		if err != nil {
			return nil, err
		}
		j.Logf("ok: %s", human(size))
		return map[string]any{"path": file, "size": size}, nil
	}), nil
}

func (r *Runner) dbRestore(ctx context.Context, args json.RawMessage) (any, error) {
	var in struct {
		DB
		File string `json:"file"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	c, err := r.target(ctx, in.DB)
	if err != nil {
		return nil, err
	}
	file, err := r.Policy.Check(in.File, false)
	if err != nil {
		return nil, err
	}
	if !r.inBackupDir(file) {
		return nil, fmt.Errorf("بازگردانی فقط از فایل‌های پوشهٔ بک‌آپ (%s) ممکن است", r.Policy.BackupDir)
	}
	if !strings.HasSuffix(file, dumpExt(in.Engine)) {
		return nil, fmt.Errorf("برای %s فایل %s لازم است", in.Engine, dumpExt(in.Engine))
	}
	d := in.DB
	return r.Start("db.restore", d.Name, func(ctx context.Context, j *Job) (any, error) {
		// A safety copy first: a wrong restore must be undoable.
		dir, err := r.ensureBackupDir("pre-restore")
		if err != nil {
			return nil, err
		}
		safety := filepath.Join(dir, fmt.Sprintf("db-%s-%s%s", d.Name, stamp(), dumpExt(d.Engine)))
		j.Logf("نسخهٔ احتیاطی پیش از بازگردانی: %s", safety)
		if _, err := dumpTo(ctx, d, c, safety); err != nil {
			return nil, fmt.Errorf("نسخهٔ احتیاطی گرفته نشد، بازگردانی انجام نشد: %v", err)
		}
		src, err := os.Open(file)
		if err != nil {
			return nil, err
		}
		defer src.Close()
		var stdin io.Reader = src
		var cmd *exec.Cmd
		switch {
		case d.Engine == "mysql":
			gz, err := gzip.NewReader(src)
			if err != nil {
				return nil, err
			}
			br := bufio.NewReaderSize(gz, 256*1024)
			if other := otherDatabase(br, d.Name); other != "" {
				return nil, fmt.Errorf("این فایل مال دیتابیس «%s» است، نه «%s»", other, d.Name)
			}
			stdin = br
			// --binary-mode: client commands such as "\! cmd" in the file are
			// not executed (a dump must not be able to run shell commands).
			if d.Where == "native" {
				a := []string{"--binary-mode"}
				if mysqlHasSandbox(ctx) {
					a = append(a, "--sandbox")
				}
				cmd = exec.CommandContext(ctx, "mysql", append(a, d.Name)...)
			} else {
				cmd = exec.CommandContext(ctx, "docker", "exec", "-i", c.name, "sh", "-c",
					`exec mysql -uroot -p"`+mysqlPw+`" --binary-mode "$0"`, d.Name)
			}
		default:
			// Restore as the database owner when that role is not a superuser,
			// so SQL in the file runs with the owner's rights only.
			role := r.pgOwner(ctx, d, c)
			a := []string{"pg_restore", "--clean", "--if-exists", "--no-owner", "-d", d.Name}
			if role != "" {
				a = append(a, "--role="+role)
			}
			if d.Where == "native" {
				cmd = exec.CommandContext(ctx, "runuser", append([]string{"-u", "postgres", "--"}, a...)...)
			} else {
				cmd = exec.CommandContext(ctx, "docker", append([]string{"exec", "-i", c.name, a[0], "-U", c.user}, a[1:]...)...)
			}
		}
		cmd.Stdin = stdin
		var out strings.Builder
		cmd.Stdout = &limited{w: &out, n: 32 * 1024}
		cmd.Stderr = cmd.Stdout
		j.Logf("بازگردانی %s از %s …", d.Name, filepath.Base(file))
		err = cmd.Run()
		if o := strings.TrimSpace(out.String()); o != "" {
			j.Logf("%s", lastLines(o, 20))
		}
		// pg_restore exits 1 for warnings it skipped ("errors ignored on
		// restore") even when the data is in; report those, don't fail.
		if err != nil && !(d.Engine == "postgres" && strings.Contains(out.String(), "errors ignored on restore")) {
			return nil, fmt.Errorf("بازگردانی ناموفق؛ نسخهٔ احتیاطی: %s", safety)
		}
		return map[string]any{"restored": d.Name, "safety": safety}, nil
	}), nil
}

var reUseDB = regexp.MustCompile("(?im)^\\s*(?:USE|CREATE\\s+(?:DATABASE|SCHEMA))\\s+(?:/\\*!\\d+\\s+IF\\s+NOT\\s+EXISTS\\s*\\*/\\s*|IF\\s+NOT\\s+EXISTS\\s+)?`?([^`;\\s]+)`?")

// otherDatabase returns the name of a different database the dump switches
// to (USE / CREATE DATABASE) near its start, or "".
func otherDatabase(br *bufio.Reader, name string) string {
	head, _ := br.Peek(256 * 1024)
	for _, m := range reUseDB.FindAllSubmatch(head, -1) {
		if string(m[1]) != name {
			return string(m[1])
		}
	}
	return ""
}

func mysqlHasSandbox(ctx context.Context) bool {
	out, _ := run(ctx, 10*time.Second, "mysql", "--help")
	return strings.Contains(out, "--sandbox")
}

// pgOwner returns the database owner if it is NOT a superuser, else "".
func (r *Runner) pgOwner(ctx context.Context, d DB, c dockerDB) string {
	q := "select r.rolname, r.rolsuper from pg_database d join pg_roles r on r.oid=d.datdba where d.datname = :'db'"
	var cmd *exec.Cmd
	if d.Where == "native" {
		cmd = exec.CommandContext(ctx, "runuser", "-u", "postgres", "--", "psql", "-XAt", "-F", "|", "-v", "db="+d.Name, "-d", "postgres")
	} else {
		cmd = exec.CommandContext(ctx, "docker", "exec", "-i", c.name, "psql", "-U", c.user, "-XAt", "-F", "|", "-v", "db="+d.Name, "-d", "postgres")
	}
	cmd.Stdin = strings.NewReader(q + ";\n")
	out, err := cmd.Output()
	if err != nil {
		return ""
	}
	f := strings.Split(strings.TrimSpace(string(out)), "|")
	if len(f) == 2 && f[1] == "f" && pgUser.MatchString(f[0]) {
		return f[0]
	}
	return ""
}
