#!/bin/sh
# certbot이 인증서를 새로 받으면(갱신 포함) web 컨테이너 nginx를 reload 해서 새 인증서를 읽게 한다.
# 설치 (서버, 레포 루트에서):
#   sudo install -m 755 deploy/certbot-deploy-hook.sh /etc/letsencrypt/renewal-hooks/deploy/mindcare-web-reload.sh
# certbot(root)이 돌리므로 sudo 없이 docker를 쓴다. 호스트 nginx는 TLS를 풀지 않아 reload할 필요 없다.
set -eu

# compose 프로젝트 mindcare의 web 서비스 (레포 경로·.env 없이 찾으려고 compose 라벨로 고른다)
ids=$(docker ps -q --filter label=com.docker.compose.project=mindcare --filter label=com.docker.compose.service=web)
if [ -z "$ids" ]; then
    echo "mindcare web 컨테이너가 떠 있지 않아 reload를 건너뜁니다 (다음에 뜰 때 새 인증서를 읽는다)"
    exit 0
fi
for id in $ids; do
    docker exec "$id" nginx -s reload
done
echo "mindcare web nginx reload 완료"
