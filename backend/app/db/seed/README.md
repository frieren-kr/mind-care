# seed — 라벨링 후보 PMID 목록

정확도 측정(라벨링)에 쓸 논문 목록을 PMID로 고정해 두는 폴더다.
검색 결과는 실행할 때마다 달라지므로, 평가용 논문은 여기 적힌 PMID로만 적재한다.

## 파일 형식

`.txt` / `.csv` / `.tsv` 파일 하나를 둔다 (예: `labeling_candidates.txt`).

- 한 줄에 PMID 하나가 기본. 쉼표·공백으로 나열해도 된다.
- `#` 뒤는 주석으로 무시한다 (제목 메모를 적어 둬도 된다).
- 중복은 자동으로 제거하고, 파일에 적힌 순서를 유지한다.

```
# 라벨링 후보 60편 (AI 담당 김현서와 합의, 2026-09-29)
39876543   # 수면장애 RCT
39876544
39876545, 39876546
```

## 적재 방법 (`backend/` 에서)

```bash
python -m scripts.seed_labeling_papers --dry-run          # 확인만
python -m scripts.seed_labeling_papers                    # 저장
python -m scripts.seed_labeling_papers --include-no-abstract   # 초록 없는 논문까지 저장
```

기본값은 **초록 없는 논문을 저장하지 않고 PMID를 목록으로 출력**한다.
평가 세트에서 빠지면 안 되는 논문이 조용히 사라지는 것을 막기 위함이다.
