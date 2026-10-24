import { useCallback, useEffect, useState } from 'react';

import { type ApiError, toApiError } from '@/api/client';

type Loader<T> = () => Promise<T>;
type Attempt<T> = { fetcher?: Loader<T>; load?: Loader<T> };
type Result<T> = { attempt?: Attempt<T>; fetcher?: Loader<T>; data: T | null; error: ApiError | null };

/**
 * API 한 개를 불러오는 훅. fetcher가 바뀌면 다시 부른다 (useCallback으로 감싸서 넘긴다).
 * - 다시 불러오는 동안에도 이전 data는 남아 있다 → 당겨서 새로고침 중에도 화면이 비지 않는다.
 * - reload(load)로 이번 한 번만 다른 호출(예: refresh=true)을 쓸 수 있다.
 */
export function useApi<T>(fetcher: Loader<T>) {
  const [attempt, setAttempt] = useState<Attempt<T>>({});
  const [result, setResult] = useState<Result<T>>({ data: null, error: null });

  useEffect(() => {
    let active = true;
    // reload 뒤에 fetcher가 바뀌었으면(예: 환자 선택 변경) 예전 reload 호출은 버린다
    const load = attempt.fetcher === fetcher && attempt.load ? attempt.load : fetcher;
    // fetcher 안에서 동기로 던진 오류(예: 사용자 id 미설정)도 실패로 받는다
    Promise.resolve().then(load).then(
      (data) => active && setResult({ attempt, fetcher, data, error: null }),
      (error) => active && setResult((prev) => ({ attempt, fetcher, data: prev.data, error: toApiError(error) })),
    );
    return () => {
      active = false;
    };
  }, [fetcher, attempt]);

  const reload = useCallback((load?: Loader<T>) => setAttempt({ fetcher, load }), [fetcher]);
  const loading = result.attempt !== attempt || result.fetcher !== fetcher;

  return { data: result.data, error: loading ? null : result.error, loading, reload };
}
