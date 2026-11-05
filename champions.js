// チャンピオンのアイコン。画像は Riot の Data Dragon から直接読む。
const DDRAGON = "https://ddragon.leagueoflegends.com";

// row は champion_names.json の champions の 1 件。version はその version
function championIcon(version, row, className) {
  const img = document.createElement("img");
  img.className = className || "icon";
  img.alt = "";
  img.loading = "lazy";
  img.decoding = "async";
  if (version && row && row.image) {
    img.src = DDRAGON + "/cdn/" + version + "/img/champion/" + row.image;
  } else {
    img.style.visibility = "hidden";
  }
  return img;
}
