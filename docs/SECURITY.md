# Security posture

Honest about what this is: a single-operator hosting panel for a small, trusted
set of clients, on a machine you control. It is not a multi-tenant cloud with a
threat model to match.

## What is done

* **Passwords** — bcrypt, cost 12.
* **Sessions** — random 32-byte tokens in SQLite, `HttpOnly`, `SameSite=Lax`,
  `Secure` when the base URL is https, 14-day expiry, listable and revocable.
* **CSRF** — a per-session token on every state-changing form and on the HTMX
  endpoints (`X-CSRF-Token`).
* **Authorisation** — every instance-scoped handler resolves ownership
  (`_owned()`) and returns the client's own rows only; admin routes go through
  `require_admin`. Cross-tenant access is refused at the handler, not in the
  template.
* **The panel is unprivileged.** It runs as an ordinary container with the Incus
  *socket* mounted. It cannot read the daemon's database and holds no
  capabilities. Everything privileged is done by `incusd` on the host.
* **No shell interpolation into a host shell.** The Incus driver executes
  `subprocess.run([...])` with an argv list; provisioning scripts are pushed
  into the container as files, never interpolated into a host command line.
* **Audit trail** — authentication, provisioning, power actions, snapshots,
  console commands, site changes, plan edits and invoice transitions all land in
  `activity` with actor and source IP.
* **Sign-in throttling and a captcha.** Six failed attempts from one address
  within five minutes close the door for the rest of the window — checked before
  the password is even compared, and a failed captcha counts as a failure too.
  When a Cloudflare Turnstile key pair is set (`ORION_TURNSTILE_SITE_KEY` and
  `ORION_TURNSTILE_SECRET`) the widget is rendered on the sign-in form and its
  token is verified from the server against `siteverify`; a Cloudflare outage
  refuses the attempt rather than waving it through. With no keys set the widget
  is absent and the check is skipped, so the panel still runs on a machine with
  no Cloudflare account. The address counted is the visitor's: behind Cloudflare
  the panel reads `CF-Connecting-IP`, which Cloudflare overwrites on every
  request, rather than `X-Forwarded-For`, which the edge rewrites.
* **A new account confirms its email address before it can be used.** The link
  carries a random token, stored only as an HMAC (never in clear), valid 48 hours
  and single-use; confirming it opens a session straight away. Sending is limited
  to five links per account per hour, so the panel cannot be used to write to
  somebody's inbox on demand. The sign-in form refuses an account that has not
  confirmed; accounts that existed before this check were marked confirmed once,
  by migration, because a rule that arrives late must not lock anybody out.
  **No mail account ships with the panel**: `ORION_MAIL_BACKEND=log` writes the
  link to the log so the flow works end to end while nothing leaves the machine,
  `sendmail` hands the message to this host's MTA, and `smtp` talks to a relay.
  The confirmation page says which of the three is in use - with `log`, that
  nobody received anything.
* **Exposure** — the panel is public, and behind Cloudflare. Its A record is
  proxied; the origin answers only Cloudflare's published ranges and the LAN, and
  403 to everything else. That is what keeps the host's own address from being
  used to bypass the proxy, and it is what makes `CF-Connecting-IP` worth
  believing. The tenant wildcard is deliberately *not* proxied and *not* gated:
  those are the clients' sites, meant to be reached directly. The Cloudflare
  ranges are fetched from `https://api.cloudflare.com/client/v4/ips`, never
  guessed; a stale list would darken the panel for everyone.
* **The gate is a `handle` block**, not a site-level `respond`, and that is not a
  matter of style. Caddy sorts a site's directives by its own default order,
  which puts the `handle` blocks first; the first matching `handle` is terminal,
  so a `respond` written above them runs after one has already answered — that
  is, never. Written that way the gate sits in the running config, looks right on
  every reading, and does nothing: measured before the fix, the panel answered
  `/healthz` to the open internet from five different networks. Every gated site
  on this host now carries its own `@external not remote_ip` line, which is worth
  remembering when editing one of them: twelve sites are gated, and the first
  line in the file belongs to none of the ones you probably meant.
* **Tenant isolation** — CPU, memory and disk are enforced by cgroups and btrfs
  qgroups. There is no overcommit by default; creation is refused when the host
  cannot carry the plan.

## Known and accepted weaknesses

1. **The web console runs commands as root in the tenant's own container.**
   That is the feature. It is scoped to a container the caller already owns and
   already has root on, so it grants nothing new — but it is worth knowing that
   a compromised session can execute commands.
2. **Generated passwords are stored in plaintext** in `instances.initial_password`
   so the panel can show them once, and WordPress credentials likewise, in
   `settings.wp_creds_<id>`. Acceptable for a proof of concept; the first
   refactor should encrypt them at rest or show them once and discard them.
3. **Shared kernel.** Containers share the host kernel. A kernel exploit escapes
   into every tenant. Use VMs (Incus supports them) if that matters.
4. **Registration is not rate limited.** The sign-in form is (see above); the
   sign-up form is not, so while `signup_open` is on a script can create accounts
   as fast as it can post.
5. **No 2FA.**
6. **Forwarded headers are trusted** for logging and the activity trail: the edge
   Caddy sets `X-Forwarded-For`, and Cloudflare sets `CF-Connecting-IP` ahead of
   it. Both are believed because the origin answers only Cloudflare's ranges and
   the LAN. Loosen that gate and the throttle stops counting anything real.

## Reporting

This is an open-source proof of concept. Open an issue rather than assuming a
private channel.
