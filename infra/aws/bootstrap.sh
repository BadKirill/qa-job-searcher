#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
umask 077
apt-get update
apt-get upgrade -y
apt-get install -y ca-certificates curl gnupg unzip docker.io docker-compose-v2 unattended-upgrades
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o /etc/apt/sources.list.d/caddy-stable.list
chmod a+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
apt-get update
apt-get install -y caddy
curl -fsSL https://awscli.amazonaws.com/v2/install.sh -o /tmp/qa-aws-install.sh
bash /tmp/qa-aws-install.sh --system --quiet
install -d -m 0700 /srv/qa-job-searcher /srv/qa-job-searcher/.runtime /var/lib/qa-job-searcher
install -d -m 0755 /etc/docker
printf '%s\n' '{"log-driver":"local","log-opts":{"max-size":"10m","max-file":"3"}}' > /etc/docker/daemon.json
systemctl enable --now docker
systemctl restart docker
if [ ! -e /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 0600 /swapfile
    mkswap /swapfile
    printf '%s\n' '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon -a
printf '%s\n' 'vm.swappiness=10' > /etc/sysctl.d/90-qa-job-searcher.conf
sysctl --system
printf '%s\n' 'APT::Periodic::Update-Package-Lists "1";' 'APT::Periodic::Unattended-Upgrade "1";' > /etc/apt/apt.conf.d/20auto-upgrades
install -d -m 0755 /etc/ssh/sshd_config.d
printf '%s\n' 'PermitRootLogin no' 'PasswordAuthentication no' > /etc/ssh/sshd_config.d/90-qa-job-searcher.conf
if systemctl is-active --quiet ssh; then
    systemctl reload ssh
fi
if ! snap list amazon-ssm-agent >/dev/null 2>&1; then
    snap install amazon-ssm-agent --classic
fi
systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service
docker compose version
aws --version
caddy version
touch /var/lib/qa-job-searcher/bootstrap-ready
