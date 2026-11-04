# 프론트 웹 빌드(expo export) → nginx로 서빙 + /api/ 를 backend로 전달. 사용법은 docs/deploy.md "웹 공개".
# 빌드 컨텍스트는 ../frontend (compose가 지정). 컨텍스트에서 뺄 것은 web.Dockerfile.dockerignore.

FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
# npm ci 대신 npm install: frontend/package-lock.json에 리눅스용 선택 의존성(@emnapi/*)이 빠져 있어 npm ci가 실패한다
RUN npm install --no-audit --no-fund
COPY . .
# EXPO_PUBLIC_* 는 빌드할 때 번들에 박힌다 (실행 중에 바꿀 수 없음 → 바꾸면 다시 빌드)
ARG EXPO_PUBLIC_API_BASE_URL=/api
ARG EXPO_PUBLIC_DEV_USER_ID
RUN test -n "$EXPO_PUBLIC_DEV_USER_ID" || { echo "EXPO_PUBLIC_DEV_USER_ID 빌드 인자가 필요합니다 (backend/.env에 넣는다)"; exit 1; }
ENV EXPO_PUBLIC_API_BASE_URL=$EXPO_PUBLIC_API_BASE_URL \
    EXPO_PUBLIC_DEV_USER_ID=$EXPO_PUBLIC_DEV_USER_ID
# app.json web.output = "static" → dist/ 에 경로별 .html이 나온다
RUN npx expo export --platform web

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
# nginx.conf·htpasswd는 compose에서 볼륨으로 붙인다 (설정만 바꿀 땐 다시 빌드하지 않아도 됨)
