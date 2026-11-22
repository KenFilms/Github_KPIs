#!/bin/zsh
# Set up and manage the GitHub traffic dashboard on a server running Caddy (Ubuntu).
# Works on a server shared with other sites: the dashboard is its own Caddy site in
# /etc/caddy/github-traffic.caddy, pulled in by one "import" line in the main Caddyfile.
#
#   ./deploy/deploy.sh setup <server-ip> [domain]  set up (safe to re-run)
#   ./deploy/deploy.sh token                       send the GitHub token to the server and collect once
#   ./deploy/deploy.sh search [key.json]           send the Google Search Console key and collect once
#   ./deploy/deploy.sh update                      push changed collect.py / dashboard.html
#   ./deploy/deploy.sh site                        rewrite the Caddy sites (dashboard + ping endpoint)
#   ./deploy/deploy.sh run                         collect now
#   ./deploy/deploy.sh status                      schedule, last runs, log
#   ./deploy/deploy.sh login                       show the dashboard URL, user + password (from Keychain)
#   ./deploy/deploy.sh password [--generate]       change the dashboard password
#   ./deploy/deploy.sh backup                      download traffic.json into ./backups/
#   ./deploy/deploy.sh remove                      take the dashboard off the server (keeps the data)
#   ./deploy/deploy.sh open | ssh                  open the dashboard / a shell on the server
#
# Without a domain the site is https://traffic.<ip-with-dashes>.sslip.io (free wildcard DNS,
# real Let's Encrypt certificate). Server details are kept in deploy/server.env.
#
# A second, public site (https://count.<ip-with-dashes>.sslip.io) receives anonymous pings
# from the GitHub Pages site (page visit, flash finished / failed, .bin link clicked). Caddy logs
# only those requests, without IP address or headers, and collect.py counts them.
#
# Caddy reloads drop open WebSocket connections of other sites on the same server (Caddy
# < 2.7). Before reloading, this script waits until Caddy has no open proxied connections;
# FORCE_RELOAD=1 skips the wait.
set -euo pipefail

DIR=${0:A:h}
ME=deploy/deploy.sh
ROOT=${DIR:h}
CONF=$DIR/server.env
KEYCHAIN_TOKEN=github-traffic-dashboard
KEYCHAIN_LOGIN=github-traffic-dashboard-login
KEYCHAIN_GSC=github-traffic-dashboard-gsc

[[ -f $CONF ]] && source $CONF
SSH_KEY=${SSH_KEY:-$HOME/.ssh/id_ed25519}
LOGIN_USER=${LOGIN_USER:-admin}
[[ -n ${SERVER_IP:-} ]] && COUNT_SITE=${COUNT_SITE:-count.${SERVER_IP//./-}.sslip.io}
PING_LOG=/var/log/caddy/github-traffic-count.log

die() { print -u2 -- "error: $*"; exit 1 }
need_conf() { [[ -n ${SERVER_IP:-} ]] || die "no server configured yet, run: $ME setup <server-ip>" }
SSH_OPTS=(-i $SSH_KEY -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
remote() { ssh $SSH_OPTS root@$SERVER_IP "$@" }
new_password() { python3 -c 'import secrets; print(secrets.token_urlsafe(15))' }

# Reload Caddy once no proxied connections (e.g. WebSockets of other sites) are open, so nobody is cut off.
safe_reload() {
  remote "FORCE=${FORCE_RELOAD:-0} bash -s" <<'EOF'
set -euo pipefail
busy() { ss -Htnp state established '( dst 127.0.0.1 or dst [::1] )' 2>/dev/null | grep -c '"caddy"' || true; }
if [ "$FORCE" != 1 ]; then
  for i in $(seq 1 24); do
    n=$(busy); [ "$n" = 0 ] && break
    [ "$i" = 1 ] && echo "    waiting: $n open proxied connection(s) on other sites (idle ones close within ~2 min)"
    sleep 10
  done
  n=$(busy)
  if [ "$n" != 0 ]; then
    echo "    still $n open proxied connection(s), probably active users on other sites. Not reloading Caddy."
    echo "    Try again later, or run with FORCE_RELOAD=1."
    exit 3
  fi
fi
systemctl reload caddy
EOF
}

push_files() {
  remote 'mkdir -p /tmp/github-traffic-deploy'
  scp -q $SSH_OPTS $ROOT/collect.py $ROOT/dashboard.html $DIR/github-traffic.service $DIR/github-traffic.timer \
    root@$SERVER_IP:/tmp/github-traffic-deploy/
  remote 'bash -s' <<'EOF'
set -euo pipefail
T=/tmp/github-traffic-deploy
# The service loads the optional Search Console key; an empty file means "not set up".
[ -e /etc/github-traffic/gsc-key.json ] || install -m 600 /dev/null /etc/github-traffic/gsc-key.json
install -m 644 $T/collect.py /opt/github-traffic/collect.py
install -m 644 $T/dashboard.html /srv/github-traffic/index.html
install -m 644 $T/github-traffic.service $T/github-traffic.timer /etc/systemd/system/
rm -rf $T
systemctl daemon-reload
EOF
}

# Hash on the server (Caddy's bcrypt) into the file the dashboard site imports.
set_password() {
  local pw=$1 hash
  hash=$(print -r -- "$pw" | remote 'IFS= read -r p; caddy hash-password --plaintext "$p"')
  [[ -n $hash && $hash != *' '* ]] || die "could not hash the password on the server"
  print -r -- "$LOGIN_USER $hash" | remote 'set -e; umask 027
    cat > /etc/caddy/github-traffic-login.caddy.new
    chgrp caddy /etc/caddy/github-traffic-login.caddy.new
    mv /etc/caddy/github-traffic-login.caddy.new /etc/caddy/github-traffic-login.caddy'
  security add-generic-password -U -a "$LOGIN_USER" -s $KEYCHAIN_LOGIN \
    -l "GitHub traffic dashboard login" -j "https://$SITE/" -w "$pw"
}

# Write the dashboard's own Caddy site, hook it into the main Caddyfile with one import line,
# validate the whole config, and roll back if it is not valid.
write_site() {
  remote 'cat > /etc/caddy/github-traffic.caddy.new' <<EOF
# GitHub traffic dashboard. Managed by deploy/deploy.sh in the Github_Dashboard project.
# Imported from /etc/caddy/Caddyfile. Login: github-traffic-login.caddy (set with: deploy.sh password)
$SITE {
	basicauth {
		import /etc/caddy/github-traffic-login.caddy
	}
	root * /srv/github-traffic
	encode gzip
	file_server
	@fresh path / /index.html /data/*
	header @fresh Cache-Control no-cache
	header {
		X-Robots-Tag "noindex, nofollow"
		Referrer-Policy no-referrer
		X-Content-Type-Options nosniff
		X-Frame-Options DENY
		-Server
	}
}

# Public ping endpoint for the GitHub Pages site (anonymous visit / flash / download counter).
# Only /view, /flash and /download are logged, and without IP address, port or headers.
$COUNT_SITE {
	@ping path /view /flash /download
	@other not path /view /flash /download
	skip_log @other
	handle @ping {
		header Access-Control-Allow-Origin "*"
		header Cache-Control no-store
		respond 204
	}
	handle {
		respond 404
	}
	header {
		X-Robots-Tag "noindex, nofollow"
		-Server
	}
	log {
		# small: only pings land here (one short line each, no personal data)
		output file $PING_LOG {
			roll_disabled
		}
		format filter {
			wrap json
			fields {
				request>remote_ip delete
				request>remote_port delete
				request>headers delete
				request>tls delete
				resp_headers delete
				user_id delete
			}
		}
	}
}

# Plain HTTP: redirect to HTTPS without logging (keeps scanners out of the ping log).
http://$COUNT_SITE {
	skip_log
	redir https://{host}{uri} 308
}
EOF
  remote "PING_LOG=$PING_LOG bash -s" <<'EOF'
set -euo pipefail
# Readable for the collector (it runs as github-traffic); never truncated once it exists.
[ -e "$PING_LOG" ] || install -m 644 -o caddy -g caddy /dev/null "$PING_LOG"
cd /etc/caddy
stamp=$(date +%Y%m%d-%H%M%S)
cp -a Caddyfile "Caddyfile.bak-github-traffic-$stamp"
[ -e github-traffic.caddy ] && cp -a github-traffic.caddy "github-traffic.caddy.bak-$stamp"
mv github-traffic.caddy.new github-traffic.caddy
if ! grep -qx 'import /etc/caddy/github-traffic.caddy' Caddyfile; then
  printf '\n# GitHub traffic dashboard (its own site; see that file)\nimport /etc/caddy/github-traffic.caddy\n' >> Caddyfile
fi
if ! caddy validate --adapter caddyfile --config Caddyfile >/tmp/caddy-validate.log 2>&1; then
  echo "    Caddy config invalid, restoring the previous one:"; grep -i error /tmp/caddy-validate.log | tail -3
  cp -a "Caddyfile.bak-github-traffic-$stamp" Caddyfile
  if [ -e "github-traffic.caddy.bak-$stamp" ]; then mv "github-traffic.caddy.bak-$stamp" github-traffic.caddy; else rm -f github-traffic.caddy; fi
  exit 1
fi
rm -f "github-traffic.caddy.bak-$stamp"
EOF
}

cmd_setup() {
  local ip=${1:-}
  [[ $ip =~ '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' ]] || die "usage: $ME setup <server-ipv4> [domain]"
  SERVER_IP=$ip
  SITE=${2:-traffic.${ip//./-}.sslip.io}
  COUNT_SITE=${3:-count.${ip//./-}.sslip.io}
  print -r -- "SERVER_IP=$SERVER_IP
SITE=$SITE
COUNT_SITE=$COUNT_SITE
SSH_KEY=$SSH_KEY
LOGIN_USER=$LOGIN_USER" > $CONF

  echo "==> Preparing $SERVER_IP"
  remote 'bash -s' <<'EOF'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
if ! command -v caddy >/dev/null; then
  # Fresh server: install Caddy and open only SSH + web. An existing setup is left alone.
  echo "    installing caddy, python3, ufw"
  apt-get update -q >/dev/null
  apt-get install -y -q caddy python3 ufw >/dev/null
  ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
elif ! command -v python3 >/dev/null; then
  apt-get update -q >/dev/null; apt-get install -y -q python3 >/dev/null
fi
id github-traffic >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin github-traffic
install -d -m 755 /opt/github-traffic /srv/github-traffic
install -d -m 755 -o github-traffic -g github-traffic /srv/github-traffic/data
install -d -m 700 /etc/github-traffic
echo "    $(. /etc/os-release; echo $PRETTY_NAME), caddy $(caddy version | cut -d' ' -f1), $(python3 --version)"
EOF

  echo "==> Installing the collector and the dashboard"
  push_files

  if ! remote 'test -s /etc/caddy/github-traffic-login.caddy'; then
    echo "==> Creating the dashboard login"
    set_password "$(new_password)"
    echo "    user $LOGIN_USER, password saved in this Mac's Keychain (show it with: $ME login)"
  fi

  echo "==> Adding the Caddy site $SITE (HTTPS + login)"
  write_site
  safe_reload

  echo "==> Waiting for the HTTPS certificate"
  local code=000 i
  for i in {1..30}; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 https://$SITE/ || true)
    [[ $code == 401 ]] && break
    sleep 3
  done
  if [[ $code == 401 ]]; then echo "    https://$SITE/ is up and asks for a login"
  else echo "    warning: https://$SITE/ answered $code (the certificate may still be on its way)"; fi

  if remote 'test -s /etc/github-traffic/token'; then
    remote 'systemctl enable --now github-traffic.timer >/dev/null 2>&1'
    echo "==> Done. Collection runs every 6 hours."
  else
    echo "==> Done. Next: $ME token"
  fi
}

cmd_token() {
  need_conf
  local tok
  tok=$(security find-generic-password -s $KEYCHAIN_TOKEN -w 2>/dev/null || true)
  if [[ -n $tok ]]; then
    echo "==> Using the GitHub token from this Mac's Keychain ($KEYCHAIN_TOKEN)"
  else
    read -rs "tok?Paste the GitHub token (input hidden): "; echo
  fi
  [[ -n $tok ]] || die "no token given"
  print -r -- "$tok" | remote 'set -e; umask 077
    cat > /etc/github-traffic/token.new
    test -s /etc/github-traffic/token.new
    mv /etc/github-traffic/token.new /etc/github-traffic/token'
  echo "==> Token stored on the server (root-only). Collecting now"
  cmd_run
  remote 'systemctl enable --now github-traffic.timer >/dev/null 2>&1'
  echo "==> Collection runs every 6 hours. Dashboard: https://$SITE/"
}

# Google Search Console key (service account, read-only) for the search terms. With a file:
# check it, keep it in this Mac's Keychain, and send it to the server. Without: resend the Keychain copy.
cmd_search() {
  need_conf
  local file=${1:-} key email
  if [[ -n $file ]]; then
    [[ -f $file ]] || die "no such file: $file"
    key=$(python3 -c 'import json, sys
k = json.load(open(sys.argv[1]))
assert k.get("type") == "service_account" and k.get("client_email") and k.get("private_key")
print(json.dumps(k, separators=(",", ":")))' "$file" 2>/dev/null) || die "$file is not a Google service-account key (JSON)"
    security add-generic-password -U -a "$USER" -s $KEYCHAIN_GSC -l "GitHub traffic dashboard: Search Console key" -w "$key"
    echo "==> Key saved in this Mac's Keychain ($KEYCHAIN_GSC). You can delete $file now."
  else
    key=$(security find-generic-password -s $KEYCHAIN_GSC -w 2>/dev/null || true)
    [[ -n $key ]] || die "usage: $ME search <service-account-key.json>"
    echo "==> Using the Search Console key from this Mac's Keychain ($KEYCHAIN_GSC)"
  fi
  email=$(print -r -- "$key" | python3 -c 'import json, sys; print(json.load(sys.stdin)["client_email"])')
  print -r -- "$key" | remote 'set -e; umask 077
    cat > /etc/github-traffic/gsc-key.json.new
    test -s /etc/github-traffic/gsc-key.json.new
    mv /etc/github-traffic/gsc-key.json.new /etc/github-traffic/gsc-key.json'
  echo "==> Key stored on the server (root-only). Service account: $email"
  echo "    Add it in Search Console (Settings > Users and permissions, permission: Restricted)."
  echo "==> Collecting now (the first run reads up to 16 months of search data)"
  cmd_run
}

cmd_run() {
  need_conf
  remote 'systemctl start github-traffic.service; rc=$?
    journalctl -u github-traffic.service --since "-3min" --no-pager -o cat | grep -v "^Start\|^Finish\|Deactivated\|Consumed" | tail -n 12
    exit $rc'
}

cmd_status() {
  need_conf
  remote 'systemctl list-timers github-traffic.timer --no-pager | head -2; echo
    journalctl -u github-traffic.service -n 8 --no-pager -o cat | grep -v "^Start\|^Finish\|Deactivated\|Consumed"
    echo; ls -l /srv/github-traffic/data/ 2>/dev/null | tail -n +2'
  echo "Dashboard: https://$SITE/"
}

cmd_password() {
  need_conf
  local pw pw2
  if [[ ${1:-} == --generate ]]; then
    pw=$(new_password)
  else
    read -rs "pw?New dashboard password for $LOGIN_USER: "; echo
    read -rs "pw2?Repeat it: "; echo
    [[ $pw == $pw2 ]] || die "the passwords differ"
    (( ${#pw} >= 10 )) || die "use at least 10 characters"
  fi
  set_password "$pw"
  safe_reload
  echo "Password changed and saved in this Mac's Keychain ($KEYCHAIN_LOGIN)."
}

cmd_remove() {
  need_conf
  remote 'bash -s' <<'EOF'
set -euo pipefail
systemctl disable --now github-traffic.timer >/dev/null 2>&1 || true
rm -f /etc/systemd/system/github-traffic.service /etc/systemd/system/github-traffic.timer
systemctl daemon-reload
cd /etc/caddy
cp -a Caddyfile "Caddyfile.bak-github-traffic-$(date +%Y%m%d-%H%M%S)"
sed -i '/^# GitHub traffic dashboard (its own site; see that file)$/d; \#^import /etc/caddy/github-traffic.caddy$#d' Caddyfile
rm -f github-traffic.caddy github-traffic-login.caddy
caddy validate --adapter caddyfile --config Caddyfile >/dev/null 2>&1
echo "    removed site and timer; data kept in /srv/github-traffic/data, token in /etc/github-traffic"
EOF
  safe_reload
}

case ${1:-} in
  setup)    shift; cmd_setup "$@" ;;
  token)    cmd_token ;;
  search)   shift; cmd_search "$@" ;;
  update)   need_conf; push_files; echo "Updated. Reload https://$SITE/" ;;
  site)     need_conf; echo "==> Writing the Caddy sites $SITE and $COUNT_SITE"; write_site; safe_reload
            code=000; for i in {1..30}; do
              code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "https://$COUNT_SITE/flash?t=1" || true)
              [[ $code == 204 ]] && break; sleep 3
            done
            echo "    ping endpoint https://$COUNT_SITE/ answered $code (204 = ready)" ;;
  run)      cmd_run ;;
  status)   cmd_status ;;
  password) shift; cmd_password "$@" ;;
  remove)   cmd_remove ;;
  login)    need_conf; echo "https://$SITE/"; echo "user:     $LOGIN_USER"
            echo "password: $(security find-generic-password -s $KEYCHAIN_LOGIN -w)" ;;
  backup)   need_conf; mkdir -p $ROOT/backups
            scp -q $SSH_OPTS root@$SERVER_IP:/srv/github-traffic/data/traffic.json $ROOT/backups/traffic-$(date +%Y-%m-%d).json
            echo "Saved backups/traffic-$(date +%Y-%m-%d).json" ;;
  open)     need_conf; open "https://$SITE/" ;;
  ssh)      need_conf; exec ssh $SSH_OPTS root@$SERVER_IP ;;
  *)        sed -n '2,/^set -euo/p' $0 | sed '$d'; exit 1 ;;
esac
