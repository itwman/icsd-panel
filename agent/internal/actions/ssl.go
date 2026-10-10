package actions

import (
	"context"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

type Cert struct {
	Name     string   `json:"name"`   // certbot lineage name or file stem
	Source   string   `json:"source"` // certbot | caddy | nginx
	Domains  []string `json:"domains"`
	Issuer   string   `json:"issuer"`
	NotAfter int64    `json:"not_after"`
	Path     string   `json:"path"`
}

type Site struct {
	File      string   `json:"file"`
	Names     []string `json:"names"`
	SSL       bool     `json:"ssl"`
	CertPath  string   `json:"cert_path,omitempty"`
	Root      string   `json:"root,omitempty"`
	ProxyPass string   `json:"proxy_pass,omitempty"`
}

func readCert(p string) (*x509.Certificate, error) {
	b, err := os.ReadFile(p)
	if err != nil {
		return nil, err
	}
	blk, _ := pem.Decode(b)
	if blk == nil {
		return nil, errors.New("no PEM")
	}
	return x509.ParseCertificate(blk.Bytes)
}

func certOf(name, source, p string) (Cert, bool) {
	c, err := readCert(p)
	if err != nil {
		return Cert{}, false
	}
	doms := c.DNSNames
	if len(doms) == 0 && c.Subject.CommonName != "" {
		doms = []string{c.Subject.CommonName}
	}
	iss := c.Issuer.CommonName
	if len(c.Issuer.Organization) > 0 {
		iss = c.Issuer.Organization[0] + " " + iss
	}
	return Cert{Name: name, Source: source, Domains: doms, Issuer: strings.TrimSpace(iss),
		NotAfter: c.NotAfter.Unix(), Path: p}, true
}

func (r *Runner) sslList(_ context.Context, _ json.RawMessage) (any, error) {
	if !r.Policy.SSL {
		return nil, errors.New("SSL در policy این سرور خاموش است")
	}
	var certs []Cert
	seen := map[string]bool{}
	add := func(c Cert) {
		key := c.Path
		if !seen[key] {
			seen[key] = true
			certs = append(certs, c)
		}
	}
	// certbot
	if items, err := os.ReadDir("/etc/letsencrypt/live"); err == nil {
		for _, it := range items {
			if !it.IsDir() {
				continue
			}
			p := filepath.Join("/etc/letsencrypt/live", it.Name(), "cert.pem")
			if c, ok := certOf(it.Name(), "certbot", p); ok {
				add(c)
			}
		}
	}
	// Caddy (in Docker volumes or on the host)
	for _, pattern := range []string{
		"/var/lib/docker/volumes/*/_data/caddy/certificates/*/*/*.crt",
		"/var/lib/caddy/.local/share/caddy/certificates/*/*/*.crt",
		"/root/.local/share/caddy/certificates/*/*/*.crt",
	} {
		matches, _ := filepath.Glob(pattern)
		for _, p := range matches {
			if c, ok := certOf(strings.TrimSuffix(filepath.Base(p), ".crt"), "caddy", p); ok {
				add(c)
			}
		}
	}
	sites := nginxSites()
	// certificates referenced by nginx but not managed above
	for _, s := range sites {
		if s.CertPath != "" && !strings.HasPrefix(s.CertPath, "/etc/letsencrypt/") {
			if c, ok := certOf(filepath.Base(s.CertPath), "nginx", s.CertPath); ok {
				add(c)
			}
		}
	}
	sort.Slice(certs, func(a, b int) bool { return certs[a].NotAfter < certs[b].NotAfter })
	return map[string]any{"certs": certs, "sites": sites, "certbot": has("certbot"), "now": time.Now().Unix()}, nil
}

func nginxSites() []Site {
	var files []string
	for _, pat := range []string{"/etc/nginx/sites-enabled/*", "/etc/nginx/conf.d/*.conf"} {
		m, _ := filepath.Glob(pat)
		files = append(files, m...)
	}
	return parseNginxFiles(files)
}

func parseNginxFiles(files []string) []Site {
	var out []Site
	for _, f := range files {
		b, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		for _, s := range parseNginxConf(string(b)) {
			s.File = f
			out = append(out, s)
		}
	}
	return out
}

// parseNginxConf tokenizes on ; { } so one-line blocks and nested
// locations read the same as nicely formatted files.
func parseNginxConf(text string) []Site {
	var lines []string
	for _, l := range strings.Split(text, "\n") {
		if i := strings.IndexByte(l, '#'); i >= 0 {
			l = l[:i]
		}
		lines = append(lines, l)
	}
	src := strings.Join(lines, " ")
	var out []Site
	var stack []string
	var cur *Site
	serverDepth := -1
	var word strings.Builder
	var words []string
	flush := func() {
		if word.Len() > 0 {
			words = append(words, word.String())
			word.Reset()
		}
	}
	for _, ch := range src {
		switch ch {
		case ' ', '\t', '\r':
			flush()
		case ';':
			flush()
			if cur != nil && len(words) > 0 {
				args := words[1:]
				switch words[0] {
				case "server_name":
					if len(stack) == serverDepth+1 {
						for _, n := range args {
							if n != "_" && n != "localhost" && n != `""` {
								cur.Names = append(cur.Names, n)
							}
						}
					}
				case "ssl_certificate":
					if len(args) > 0 {
						cur.CertPath, cur.SSL = args[0], true
					}
				case "listen":
					for _, a := range args {
						if a == "ssl" || a == "443" || strings.HasSuffix(a, ":443") {
							cur.SSL = true
						}
					}
				case "root":
					if len(args) > 0 && cur.Root == "" {
						cur.Root = args[0]
					}
				case "proxy_pass":
					if len(args) > 0 && cur.ProxyPass == "" {
						cur.ProxyPass = args[0]
					}
				}
			}
			words = nil
		case '{':
			flush()
			if len(words) > 0 && words[0] == "server" && cur == nil {
				cur = &Site{}
				serverDepth = len(stack)
			}
			name := ""
			if len(words) > 0 {
				name = words[0]
			}
			stack = append(stack, name)
			words = nil
		case '}':
			flush()
			words = nil
			if len(stack) == 0 {
				continue
			}
			stack = stack[:len(stack)-1]
			if cur != nil && len(stack) == serverDepth {
				if len(cur.Names) > 0 {
					out = append(out, *cur)
				}
				cur, serverDepth = nil, -1
			}
		default:
			word.WriteRune(ch)
		}
	}
	return out
}

var (
	domainRe = regexp.MustCompile(`^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$`)
	emailRe  = regexp.MustCompile(`^[^@\s]+@[^@\s]+\.[a-z]{2,}$`)
	certName = regexp.MustCompile(`^[A-Za-z0-9.-]{1,128}$`)
)

func (r *Runner) sslRenew(_ context.Context, args json.RawMessage) (any, error) {
	if !r.Policy.SSL || !has("certbot") {
		return nil, errors.New("certbot روی این سرور نیست یا SSL در policy خاموش است")
	}
	var in struct {
		Name  string `json:"name"`
		Force bool   `json:"force"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	if !certName.MatchString(in.Name) {
		return nil, errors.New("نام گواهی نامعتبر")
	}
	return r.Start("ssl.renew", in.Name, func(ctx context.Context, j *Job) (any, error) {
		a := []string{"renew", "--cert-name", in.Name, "--non-interactive"}
		if in.Force {
			a = append(a, "--force-renewal")
		}
		out, err := run(ctx, 5*time.Minute, "certbot", a...)
		j.Logf("%s", lastLines(out, 30))
		if err != nil {
			return nil, err
		}
		return map[string]any{"name": in.Name}, nil
	}), nil
}

// sslIssue gets a certificate for domains already served by an nginx site
// (certbot edits that site to add HTTPS).
func (r *Runner) sslIssue(_ context.Context, args json.RawMessage) (any, error) {
	if !r.Policy.SSL || !has("certbot") {
		return nil, errors.New("certbot روی این سرور نیست یا SSL در policy خاموش است")
	}
	var in struct {
		Domains  []string `json:"domains"`
		Email    string   `json:"email"`
		Redirect bool     `json:"redirect"`
	}
	if err := decode(args, &in); err != nil {
		return nil, err
	}
	if len(in.Domains) == 0 || len(in.Domains) > 20 {
		return nil, errors.New("دامنه‌ها را مشخص کنید")
	}
	a := []string{"--nginx", "--non-interactive", "--agree-tos", "--keep-until-expiring"}
	for _, d := range in.Domains {
		d = strings.ToLower(strings.TrimSpace(d))
		if !domainRe.MatchString(d) || strings.HasPrefix(d, "*.") {
			return nil, errors.New("دامنهٔ نامعتبر: " + d)
		}
		a = append(a, "-d", d)
	}
	if in.Email != "" {
		if !emailRe.MatchString(in.Email) {
			return nil, errors.New("ایمیل نامعتبر")
		}
		a = append(a, "-m", in.Email)
	} else {
		a = append(a, "--register-unsafely-without-email")
	}
	if in.Redirect {
		a = append(a, "--redirect")
	}
	return r.Start("ssl.issue", strings.Join(in.Domains, " "), func(ctx context.Context, j *Job) (any, error) {
		out, err := run(ctx, 5*time.Minute, "certbot", a...)
		j.Logf("%s", lastLines(out, 30))
		if err != nil {
			return nil, err
		}
		return map[string]any{"domains": in.Domains}, nil
	}), nil
}
