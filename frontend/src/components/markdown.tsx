import { type ReactNode } from 'react';
import { Linking, Platform, StyleSheet, Text, View } from 'react-native';

import { keepKoreanWordsText, ThemedText } from '@/components/themed-text';
import { FontFamily, Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

/**
 * 챗봇 답변용 작은 마크다운 렌더러. AI가 흔히 쓰는 문법만 그린다:
 * 제목(#), 굵게(**), 기울임(*), `코드`, 링크, 글머리표(- * •)·번호 목록(들여쓰기 = 하위 목록), 인용(>), 구분선(---), 표(|), 코드 블록(```).
 * 라이브러리 대신 직접 그려서 앱 글꼴·글자 크기 설정·한글 어절 줄바꿈(ThemedText)을 그대로 쓴다.
 * 모르는 문법은 글자 그대로 보여 준다 (내용이 사라지지 않게).
 */

type Block =
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'paragraph'; text: string }
  | { kind: 'item'; ordered: boolean; marker: string; depth: number; text: string }
  | { kind: 'quote'; text: string }
  | { kind: 'rule' }
  | { kind: 'code'; text: string }
  | { kind: 'table'; header: string[]; rows: string[][] };

const BULLET = /^(\s*)[-*+•]\s+(.*)$/;
const ORDERED = /^(\s*)(\d{1,3})[.)]\s+(.*)$/;
const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_DIVIDER = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function cells(line: string): string[] {
  return line.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
}

/** 들여쓰기 칸 수 → 목록 깊이 (2~4칸 = 한 단계, 탭 = 한 단계) */
function depthOf(indent: string): number {
  const width = indent.replace(/\t/g, '    ').length;
  return Math.min(3, Math.floor(width / 2));
}

export function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n/g, '\n').split('\n');
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  const flush = () => {
    if (paragraph.length) blocks.push({ kind: 'paragraph', text: paragraph.join('\n') });
    paragraph = [];
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let m: RegExpMatchArray | null;

    if (line.trim().startsWith('```')) {
      flush();
      const code: string[] = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith('```'); i++) code.push(lines[i]);
      blocks.push({ kind: 'code', text: code.join('\n') });
      continue;
    }
    if (!line.trim()) {
      flush();
      continue;
    }
    if (TABLE_ROW.test(line) && i + 1 < lines.length && TABLE_DIVIDER.test(lines[i + 1])) {
      flush();
      const header = cells(line);
      const rows: string[][] = [];
      for (i += 2; i < lines.length && TABLE_ROW.test(lines[i]); i++) rows.push(cells(lines[i]));
      i--;
      blocks.push({ kind: 'table', header, rows });
      continue;
    }
    if ((m = line.match(HEADING))) {
      flush();
      blocks.push({ kind: 'heading', level: m[1].length, text: m[2] });
    } else if (RULE.test(line)) {
      flush();
      blocks.push({ kind: 'rule' });
    } else if ((m = line.match(ORDERED))) {
      flush();
      blocks.push({ kind: 'item', ordered: true, marker: `${m[2]}.`, depth: depthOf(m[1]), text: m[3] });
    } else if ((m = line.match(BULLET))) {
      flush();
      blocks.push({ kind: 'item', ordered: false, marker: '•', depth: depthOf(m[1]), text: m[2] });
    } else if (line.trimStart().startsWith('>')) {
      flush();
      const text = line.trimStart().replace(/^>\s?/, '');
      const last = blocks[blocks.length - 1];
      if (last?.kind === 'quote') last.text += `\n${text}`;
      else blocks.push({ kind: 'quote', text });
    } else {
      // 목록 항목 바로 아래 들여쓴 줄은 그 항목의 이어지는 글
      const last = blocks[blocks.length - 1];
      if (!paragraph.length && last?.kind === 'item' && /^\s{2,}\S/.test(line)) last.text += `\n${line.trim()}`;
      else paragraph.push(line);
    }
  }
  flush();
  return blocks;
}

// 코드 → 링크 → 굵게 → 기울임 순서로 찾는다. 기울임은 *만 (단어 속 _는 그대로 두려고)
const INLINE = /`([^`\n]+)`|\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)|\*\*(.+?)\*\*|__(.+?)__|(?<![*\w])\*(?!\s)([^*\n]+?)\*(?![*\w])/g;

/**
 * 한 줄 안의 강조·코드·링크. 바깥 ThemedText 안에 중첩 Text로 넣어서 글자 크기·색은 바깥을 따른다
 * (표 칸처럼 작은 글씨 안에서도 같은 크기로)
 */
function Inline({ text }: { text: string }) {
  const theme = useTheme();
  const parts: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) parts.push(keepKoreanWordsText(text.slice(last, at)));
    const [, code, linkText, url, bold, bold2, italic] = m;
    const key = parts.length;
    if (code !== undefined) {
      parts.push(
        // 한글이 많아 고정폭 글꼴 대신 바탕색으로만 구분 (고정폭은 한글 글자 사이가 벌어진다)
        <Text key={key} style={{ backgroundColor: theme.backgroundSelected }}>
          {keepKoreanWordsText(code)}
        </Text>,
      );
    } else if (linkText !== undefined && url) {
      parts.push(
        <Text
          key={key}
          accessibilityRole="link"
          style={[styles.link, { color: theme.primary }]}
          onPress={() => Linking.openURL(url)}>
          {keepKoreanWordsText(linkText)}
        </Text>,
      );
    } else if (bold !== undefined || bold2 !== undefined) {
      parts.push(
        <Text key={key} style={styles.bold}>
          <Inline text={(bold ?? bold2) as string} />
        </Text>,
      );
    } else if (italic !== undefined) {
      parts.push(
        <Text key={key} style={styles.italic}>
          {keepKoreanWordsText(italic)}
        </Text>,
      );
    }
    last = at + m[0].length;
  }
  if (last < text.length) parts.push(keepKoreanWordsText(text.slice(last)));
  return <>{parts}</>;
}

function Line({ text, type }: { text: string; type?: 'default' | 'bold' | 'subtitle' }) {
  return (
    <ThemedText type={type}>
      <Inline text={text} />
    </ThemedText>
  );
}

export function Markdown({ text }: { text: string }) {
  const theme = useTheme();
  const blocks = parseBlocks(text);

  return (
    <View style={styles.root}>
      {blocks.map((b, i) => {
        switch (b.kind) {
          case 'heading':
            return <Line key={i} text={b.text} type={b.level <= 2 ? 'subtitle' : 'bold'} />;
          case 'paragraph':
            return <Line key={i} text={b.text} />;
          case 'item':
            return (
              <View key={i} style={[styles.item, { paddingLeft: b.depth * Spacing.three }]}>
                <ThemedText type={b.ordered ? 'bold' : 'default'} style={styles.marker}>
                  {b.depth > 0 && !b.ordered ? '◦' : b.marker}
                </ThemedText>
                <View style={styles.flex}>
                  <Line text={b.text} />
                </View>
              </View>
            );
          case 'quote':
            return (
              <View key={i} style={[styles.quote, { borderLeftColor: theme.rule }]}>
                <ThemedText themeColor="textSecondary">
                  <Inline text={b.text} />
                </ThemedText>
              </View>
            );
          case 'rule':
            return <View key={i} style={[styles.rule, { backgroundColor: theme.border }]} />;
          case 'code':
            return (
              <View key={i} style={[styles.codeBlock, { backgroundColor: theme.backgroundSelected }]}>
                <ThemedText type="small" style={styles.mono}>
                  {b.text}
                </ThemedText>
              </View>
            );
          case 'table':
            return (
              <View key={i} style={[styles.table, { borderColor: theme.border }]}>
                {[b.header, ...b.rows].map((row, r) => (
                  <View
                    key={r}
                    style={[
                      styles.tableRow,
                      { borderTopColor: theme.border, borderTopWidth: r === 0 ? 0 : StyleSheet.hairlineWidth },
                      r === 0 && { backgroundColor: theme.backgroundSelected },
                    ]}>
                    {b.header.map((_, c) => (
                      <View key={c} style={styles.tableCell}>
                        <ThemedText type={r === 0 ? 'smallBold' : 'small'}>
                          <Inline text={row[c] ?? ''} />
                        </ThemedText>
                      </View>
                    ))}
                  </View>
                ))}
              </View>
            );
        }
        return null;
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  root: {
    gap: Spacing.two,
  },
  item: {
    flexDirection: 'row',
    gap: Spacing.two,
  },
  marker: {
    minWidth: 18,
  },
  flex: {
    flex: 1,
  },
  quote: {
    borderLeftWidth: 3,
    paddingLeft: Spacing.three,
  },
  rule: {
    height: StyleSheet.hairlineWidth,
    marginVertical: Spacing.one,
  },
  mono: {
    fontFamily: Platform.select({ ios: 'Menlo', android: 'monospace', default: 'monospace' }),
  },
  bold: {
    fontFamily: FontFamily.bold,
  },
  codeBlock: {
    borderRadius: Radius.card,
    padding: Spacing.three,
  },
  link: {
    fontFamily: FontFamily.bold,
    textDecorationLine: 'underline',
  },
  italic: {
    fontStyle: 'italic',
  },
  table: {
    borderWidth: StyleSheet.hairlineWidth,
  },
  tableRow: {
    flexDirection: 'row',
  },
  tableCell: {
    flex: 1,
    padding: Spacing.two,
  },
});
