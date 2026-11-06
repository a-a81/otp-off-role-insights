// 画面の文言（日本語・英語・韓国語）。index.html と result.html で共有する。
const I18N = {
  languages: { ja: "日本語", en: "English", ko: "한국어" },
  fallback: "en",
  strings: {
    ja: {
      siteName: "OTP Off-Role Insights",
      language: "言語",
      chooseHeading: "メインレーンとチャンピオンを選ぶ",
      region: "リージョン",
      regionAll: "JP+KR+NA 合計",
      mainLane: "メインレーン",
      champion: "チャンピオン",
      championPlaceholder: "ヤスオ / Yasuo",
      search: "検索",
      championNotFound: "チャンピオンが見つかりません。候補から選んでください。",
      namesLoadFailed: "チャンピオン一覧を読み込めませんでした。",
      back: "← チャンピオン選択に戻る",
      profile: "集計対象",
      profileMain: "メイン",
      profileOtp: "OTP",
      profileNote: "メインレーンでそのチャンピオンの使用率が {rate}% 以上のプレイヤーを集計しています。",
      resultHeading: "{lane} {champion} {profile}（{region}）",
      summary: "対象プレイヤー: {players} 人 / 更新: {updated}",
      loading: "読み込み中…",
      noData: "この条件のデータはまだありません。",
      mainLaneHeading: "メインレーン: {lane}",
      mainLaneNext: "指定チャンピオンの次に使用率が高いチャンピオン",
      otherLanes: "その他のレーンに回ったとき",
      games: "（{games} 試合）",
      empty: "データなし",
      legal: "OTP Off-Role Insights は Riot Games の承認を受けたものではなく、Riot Games または Riot Games のプロパティの制作・運営に公式に関わる人物の見解や意見を反映するものではありません。Riot Games およびすべての関連プロパティは、Riot Games, Inc. の商標または登録商標です。",
    },
    en: {
      siteName: "OTP Off-Role Insights",
      language: "Language",
      chooseHeading: "Choose a main lane and champion",
      region: "Region",
      regionAll: "JP+KR+NA combined",
      mainLane: "Main lane",
      champion: "Champion",
      championPlaceholder: "Yasuo",
      search: "Search",
      championNotFound: "Champion not found. Please pick one from the suggestions.",
      namesLoadFailed: "Could not load the champion list.",
      back: "← Back to champion selection",
      profile: "Players",
      profileMain: "Mains",
      profileOtp: "OTPs",
      profileNote: "Players who pick this champion in {rate}%+ of their main-lane games.",
      resultHeading: "{lane} {champion} {profile} ({region})",
      summary: "Players: {players} / Updated: {updated}",
      loading: "Loading…",
      noData: "No data for this selection yet.",
      mainLaneHeading: "Main lane: {lane}",
      mainLaneNext: "Most played champions after the selected one",
      otherLanes: "When placed in other lanes",
      games: "({games} games)",
      empty: "No data",
      // 英語は下の公式文がそのまま出るので訳は出さない
      legal: "",
    },
    ko: {
      siteName: "OTP Off-Role Insights",
      language: "언어",
      chooseHeading: "주 라인과 챔피언 선택",
      region: "지역",
      regionAll: "JP+KR+NA 합계",
      mainLane: "주 라인",
      champion: "챔피언",
      championPlaceholder: "야스오 / Yasuo",
      search: "검색",
      championNotFound: "챔피언을 찾을 수 없습니다. 목록에서 선택해 주세요.",
      namesLoadFailed: "챔피언 목록을 불러오지 못했습니다.",
      back: "← 챔피언 선택으로 돌아가기",
      profile: "집계 대상",
      profileMain: "주챔",
      profileOtp: "원챔",
      profileNote: "주 라인에서 이 챔피언 사용률이 {rate}% 이상인 플레이어를 집계했습니다.",
      resultHeading: "{lane} {champion} {profile} ({region})",
      summary: "대상 플레이어: {players}명 / 업데이트: {updated}",
      loading: "불러오는 중…",
      noData: "이 조건의 데이터가 아직 없습니다.",
      mainLaneHeading: "주 라인: {lane}",
      mainLaneNext: "선택한 챔피언 다음으로 많이 쓰는 챔피언",
      otherLanes: "다른 라인에 배정됐을 때",
      games: "({games}게임)",
      empty: "데이터 없음",
      legal: "OTP Off-Role Insights는 Riot Games의 보증을 받지 않았으며, Riot Games 또는 Riot Games 자산의 제작·관리에 공식적으로 관여하는 누구의 견해나 의견도 반영하지 않습니다. Riot Games 및 모든 관련 자산은 Riot Games, Inc.의 상표 또는 등록 상표입니다.",
    },
  },
  // データ上のレーン名 → 表示名
  lanes: {
    ja: { TOP: "TOP", JUNGLE: "JUNGLE", MID: "MID", BOTTOM: "ADC", UTILITY: "SUPPORT" },
    en: { TOP: "TOP", JUNGLE: "JUNGLE", MID: "MID", BOTTOM: "ADC", UTILITY: "SUPPORT" },
    ko: { TOP: "탑", JUNGLE: "정글", MID: "미드", BOTTOM: "원딜", UTILITY: "서포터" },
  },
};

function readStoredLang() {
  try {
    return localStorage.getItem("lang");
  } catch (error) {
    return null;
  }
}

function storeLang(lang) {
  try {
    localStorage.setItem("lang", lang);
  } catch (error) {
    // 保存できなくても、このページでは切り替える
  }
}

function detectLang() {
  const fromUrl = new URLSearchParams(location.search).get("lang");
  if (I18N.strings[fromUrl]) {
    // 次のページ（URL に lang がない）でも同じ言語にする
    storeLang(fromUrl);
    return fromUrl;
  }
  const candidates = [
    readStoredLang(),
    ...(navigator.languages || [navigator.language || ""]).map((tag) => tag.slice(0, 2)),
  ];
  return candidates.find((lang) => lang && I18N.strings[lang]) || "ja";
}

let currentLang = detectLang();

function t(key, vars) {
  const table = I18N.strings[currentLang];
  const template = table[key] !== undefined ? table[key] : I18N.strings[I18N.fallback][key];
  return template.replace(/\{(\w+)\}/g, (match, name) =>
    vars && vars[name] !== undefined ? vars[name] : match
  );
}

function laneLabel(lane) {
  return I18N.lanes[currentLang][lane] || lane;
}

// names は champion_names.json の names（{ ja, en, ko }）
function championName(names, id) {
  if (!names) return id;
  return names[currentLang] || names[I18N.fallback] || id;
}

function applyStaticText() {
  document.documentElement.lang = currentLang;
  for (const element of document.querySelectorAll("[data-i18n]")) {
    element.textContent = t(element.dataset.i18n);
    if (element.hasAttribute("data-i18n-optional")) element.hidden = !element.textContent;
  }
  for (const element of document.querySelectorAll("[data-i18n-placeholder]")) {
    element.placeholder = t(element.dataset.i18nPlaceholder);
  }
}

function mountLanguageSelect(onChange) {
  const select = document.getElementById("lang-select");
  for (const [code, label] of Object.entries(I18N.languages)) {
    const option = document.createElement("option");
    option.value = code;
    option.textContent = label;
    select.append(option);
  }
  select.value = currentLang;
  select.addEventListener("change", () => {
    currentLang = select.value;
    storeLang(currentLang);
    applyStaticText();
    if (onChange) onChange();
  });
  applyStaticText();
}
