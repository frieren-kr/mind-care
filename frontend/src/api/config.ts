import { Platform } from 'react-native';

/**
 * 서버 주소. EXPO_PUBLIC_API_BASE_URL이 있으면 그 값을 쓴다 (frontend/.env.local, .env.example 참고).
 * 없으면 안드로이드 에뮬레이터는 10.0.2.2(PC의 localhost), 웹·iOS 시뮬레이터는 localhost.
 * 실기기는 PC IP를 직접 넣어야 하고, 백엔드도 `uvicorn main:app --reload --host 0.0.0.0`으로 띄운다.
 */
const DEFAULT_API_URL = Platform.OS === 'android' ? 'http://10.0.2.2:8000' : 'http://localhost:8000';

export const API_URL = (process.env.EXPO_PUBLIC_API_BASE_URL || DEFAULT_API_URL).replace(/\/+$/, '');

/**
 * 로그인 기능 전까지 쓰는 간병인 id (users.id).
 * backend/에서 `python -m scripts.list_users`로 확인해 EXPO_PUBLIC_DEV_USER_ID에 넣는다.
 */
export const DEV_USER_ID = process.env.EXPO_PUBLIC_DEV_USER_ID?.trim() || null;
