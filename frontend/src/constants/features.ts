/**
 * 백엔드 API가 없어 잠시 숨겨 둔 기능 (11/18). 화면 코드는 그대로 두었으니, API가 생기면 true로 바꾸면 다시 보인다.
 * - signup: 회원가입 (POST /auth/signup) — 시연은 미리 만든 계정으로 해서 이번 범위에서 뺐다
 * - editName: 내 정보 이름(닉네임) 수정 (PATCH /users/{user_id})
 */
export const FEATURES = {
  signup: false,
  editName: false,
} as const;
