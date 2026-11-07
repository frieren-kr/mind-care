import { ScrollViewStyleReset } from 'expo-router/html';
import type { PropsWithChildren } from 'react';

/**
 * 웹 화면의 HTML 뼈대 (웹에서만 쓰인다). 기본값은 lang="en"이라 크롬이 "영어 페이지를 번역할까요?"를 띄워서
 * 한국어로 지정한다 (11/7 휴대폰 크롬 테스트에서 발견).
 */
export default function Root({ children }: PropsWithChildren) {
  return (
    <html lang="ko">
      <head>
        <meta charSet="utf-8" />
        <meta httpEquiv="X-UA-Compatible" content="IE=edge" />
        <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no" />
        {/* 웹에서 ScrollView가 화면 전체를 쓰도록 body 스크롤을 끈다 (expo-router 기본 설정과 같다) */}
        <ScrollViewStyleReset />
      </head>
      <body>{children}</body>
    </html>
  );
}
