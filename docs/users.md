# 사용자·환자 (김한슬 영역)

> 담당: **김한슬**. 이 문서는 루트 `AGENTS.md`에 있던 문장을 그대로 옮긴 것이다 — 고칠 때는 담당자가 고친다.
> API 주소 표는 [`api.md`](api.md), 피드 개인화 조회(`feed_read.py`, 박주현)는 [`db.md`](db.md).

- 담당 테이블: `users`, `patient_profiles`, `caregiver_profiles`, `clinical_assessments`, `safety_events`, `medications`, `medical_visits`
- `/feed…` 주소 3개(담당: 김한슬)는 **간병인·환자 정보 조회(피드·챗봇 개인화 입력용)**다. 이름은 feed지만
  논문을 돌려주지 않는다 — **논문 추천은 `/recommendations`**. 응답 형식의 정본은 `backend/app/user_schemas.py`.
- 사용자·환자 함수 목록은 `app/db/user_functions.py` 참고 (담당: 김한슬).
