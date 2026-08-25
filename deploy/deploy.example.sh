#!/usr/bin/env bash
#
# Putting the dataset viewer/editor on a hostname, start to finish. Read it
# before running it: it edits nginx's config and enables a systemd unit, and
# the last step publishes an app that can delete other people's work.
#
# Run from the checkout root:  bash deploy/deploy.example.sh
set -euo pipefail

# A name that already resolves to this machine. sslip.io encodes the address
# in the name, so it needs no DNS panel and no waiting.
HOST="${HOST:-diar-dis.34.124.243.65.sslip.io}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO"

# 1. The name has to point here, or certbot cannot validate it later.
echo "==> $HOST resolves to: $(dig +short A "$HOST" @8.8.8.8)"
echo "    (that must be this machine's address)"

# 2. The app itself, on localhost, kept up across reboots.
sudo cp deploy/diarization-display.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now diarization-display
sudo systemctl --no-pager --lines=0 status diarization-display
curl -fsS -o /dev/null -w '   localhost: %{http_code}\n' http://127.0.0.1:8200/

# 3. nginx in front of it. The vhost ships with a placeholder server_name.
sudo cp deploy/nginx.conf.example /etc/nginx/conf.d/diar-dis.conf
sudo sed -i "s/diar-dis\.blaze\.vn/$HOST/" /etc/nginx/conf.d/diar-dis.conf
sudo nginx -t
sudo systemctl reload nginx
curl -fsS -o /dev/null -w '   over http: %{http_code}\n' "http://$HOST/"

# 4. TLS. certbot rewrites the vhost in place and adds the :443 server block.
sudo certbot --nginx -d "$HOST"
curl -fsS -o /dev/null -w '   over https: %{http_code}\n' "https://$HOST/"

cat <<NEXT

Deployed, and nobody can change anything yet -- including you. That is the
default, not a fault: editing is granted per browser in

    $REPO/whitelist.csv

Open https://$HOST/, copy the id at the top right of the page, and add it:

    echo '<the id>,Duy' >> $REPO/whitelist.csv

then refresh. No restart: the file is re-read on the next request. The same
two columns in blacklist.csv refuse a browser the page outright.
NEXT
