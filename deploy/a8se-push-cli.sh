#!/bin/sh
# Atalho para operar o worker via SSH com o mesmo usuário e ambiente do serviço.
# Instalado em /usr/local/bin/a8se-push. Uso: sudo a8se-push status
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo "use: sudo a8se-push $*" >&2
    exit 1
fi
cd /var/lib/a8se-push
exec runuser -u a8se-push -- env \
    GOOGLE_APPLICATION_CREDENTIALS=/etc/a8se-push/service-account.json \
    A8SE_PUSH_CONFIG=/etc/a8se-push/config.toml \
    /opt/a8se-push/venv/bin/a8se-push "$@"
