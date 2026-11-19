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
  no Cloudflare account.
* **Exposure** — the panel's own hostname is gated to the LAN by the edge Caddy;
  anything else gets 403. The tenant wildcard is deliberately *not* gated: those
  are the clients' sites. The gate lives inside a `handle` block because a
  site-level `respond` is reordered after the `handle`s by Caddy's default
  directive order and never runs — measured before that fix, the panel answered
  `/healthz` to the open internet from five different networks.
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
5. **Behind a proxy, the throttle follows the proxy.** `client_ip()` reads
   `X-Forwarded-For`, which the edge Caddy sets. Putting the panel behind
   Cloudflare without also reading `CF-Connecting-IP` would collapse every
   visitor onto one address — and then one attacker could close the door for
   everybody. Read that header before switching the DNS record to proxied.
6. **No 2FA.**
7. **`X-Forwarded-For` is trusted** for the purpose of logging and the activity
   trail. It is set by the edge Caddy, which is the only path in.

## Reporting

This is an open-source proof of concept. Open an issue rather than assuming a
private channel.
