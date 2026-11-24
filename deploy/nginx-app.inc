# web 컨테이너 nginx의 앱 부분. nginx.conf(1단계 HTTP)·nginx.https.conf(2단계 HTTPS)의 server 블록이 include 한다.
# / → 프론트 정적 파일(기본 인증), /api/ → backend:8000 (접두사 /api 제거, 기본 인증 없음).
client_max_body_size 1m;

auth_basic "Mind Care";
auth_basic_user_file /etc/nginx/htpasswd;

root /usr/share/nginx/html;

location / {
    # expo static 출력: /feed → feed.html. 없는 경로·동적 경로는 index.html로 → 앱이 라우팅
    try_files $uri $uri.html $uri/ /index.html;
}

# /paper/123 → 미리 만들어진 paper/[id].html
location ~ ^/paper/[^/]+/?$ {
    try_files "/paper/[id].html" /index.html;
}

location /_expo/ {
    expires 1y;
    add_header Cache-Control "public, immutable";
}

# Swagger·API 목록은 밖에 보이지 않는다 (서버에서 SSH 터널로 127.0.0.1:8000/docs)
location ~ ^/api/(docs|redoc|openapi\.json) {
    return 404;
}

# 끝의 / 가 /api 접두사를 뗀다: /api/papers?x → backend:8000/papers?x
# API는 기본 인증 대신 백엔드 로그인(Bearer 토큰)으로 막는다. 기본 인증과 Bearer가 같은 Authorization
# 헤더를 쓰므로 여기서 기본 인증을 끄고, 앱이 보낸 Authorization을 그대로 백엔드에 넘긴다.
location /api/ {
    auth_basic off;
    proxy_pass http://backend:8000/;
    proxy_set_header Host $host;
    # 클라이언트가 보낸 X-Forwarded-For는 버리고 실제 접속 IP($remote_addr)로 덮어쓴다.
    # 1단계(80): 접속한 주소 그대로. 2단계(8443): 호스트 nginx가 PROXY protocol로 넘긴 주소
    #   (nginx.https.conf의 real_ip 설정이 도커 게이트웨이에서 온 연결에서만 $remote_addr를 그 값으로 바꾼다).
    # 백엔드 uvicorn은 이 nginx(고정 IP)가 보낸 값만 믿는다 → 로그인 횟수 제한이 사용자 IP 기준이 된다.
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;   # 2단계에서는 https
    proxy_read_timeout 120s;            # AI 피드가 30~60초 걸린다
}
