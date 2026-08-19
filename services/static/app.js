const folderInput = document.getElementById("folder-input");
const sourceStatus = document.getElementById("source-status");
const splitTabsEl = document.getElementById("split-tabs");
const searchInput = document.getElementById("search-input");
const filterToggle = document.getElementById("anomaly-filter-toggle");
const filterOptionsEl = document.getElementById("anomaly-filter-options");
const resultListEl = document.getElementById("result-list");
const answerEl = document.getElementById("result");

// splitName -> { records: Map(kayitId -> record), pairGroups: Map(pairKey -> [kayitId,...]),
//                turSayaci: Map(tur -> count) }
let splits = {};
let activeSplit = null;
const activeAnomaliFilter = new Set(); // secili tur adlari (coklu secim)
let selectedKayitId = null;

function pairKeyOf(rec) {
  return `${rec.satici_vkn}::${rec.fatura_no}`;
}

function kayitIdNormalize(input) {
  const temiz = input.trim().replace(/^[Kk]/, "");
  if (!/^\d+$/.test(temiz)) return null;
  return "K" + temiz.padStart(7, "0");
}

// --- Klasör yükleme -----------------------------------------------------

folderInput.addEventListener("change", async (event) => {
  const files = Array.from(event.target.files);
  const girdiFiles = files.filter((f) => f.name.endsWith("_girdi.json"));

  splits = {};
  let atlananKisiselBakim = 0;

  for (const girdiFile of girdiFiles) {
    const splitAdi = girdiFile.name.replace(/_girdi\.json$/, "");
    const etiketFile = files.find((f) => f.name === `${splitAdi}_etiket.json`);
    if (!etiketFile) {
      console.warn(`${splitAdi}: eşleşen _etiket.json bulunamadı, atlanıyor.`);
      continue;
    }

    let girdi, etiket;
    try {
      girdi = JSON.parse(await girdiFile.text());
      etiket = JSON.parse(await etiketFile.text());
    } catch (err) {
      console.warn(`${splitAdi} okunamadı:`, err);
      continue;
    }

    const etiketMap = new Map(etiket.map((e) => [e.kayit_id, e]));
    const records = new Map();
    const pairGroups = new Map();
    const turSayaci = new Map();

    for (const g of girdi) {
      const e = etiketMap.get(g.kayit_id);
      if (!e) continue;
      if (e.is_kolu === "kisisel_bakim") {
        atlananKisiselBakim++;
        continue;
      }

      const data = {
        ...g,
        is_kolu: e.is_kolu,
        is_anomali: e.is_anomali,
        anomali_turleri: e.anomali_turleri,
        aciklama_kategorisi: e.aciklama_kategorisi,
        kalemler: g.kalemler.map((k, i) => ({ ...k, harcama_kategorisi: e.harcama_kategorileri[i] })),
      };
      const rec = {
        kayitId: g.kayit_id, data, pairKey: pairKeyOf(g),
        saticiUnvan: g.satici_unvan, genelToplam: g.genel_toplam,
        anomaliTurleri: e.anomali_turleri,
      };
      records.set(rec.kayitId, rec);

      if (!pairGroups.has(rec.pairKey)) pairGroups.set(rec.pairKey, []);
      pairGroups.get(rec.pairKey).push(rec.kayitId);

      for (const t of e.anomali_turleri) turSayaci.set(t, (turSayaci.get(t) || 0) + 1);
    }

    splits[splitAdi] = { records, pairGroups, turSayaci };
  }

  renderSplitTabs();
  const splitAdlari = Object.keys(splits);
  if (splitAdlari.length) {
    const yuklenen = splitAdlari.reduce((a, s) => a + splits[s].records.size, 0);
    const toplam = yuklenen + atlananKisiselBakim;
    sourceStatus.textContent = `${splitAdlari.length} bölme, ${toplam} kayıttan ${yuklenen}'i yüklendi` +
      (atlananKisiselBakim ? ` (${atlananKisiselBakim}'i eski is_kolu=kisisel_bakim olduğu için atlandı)` : "");
    searchInput.disabled = false;
    filterToggle.disabled = false;
    setActiveSplit(splitAdlari[0]);
  } else {
    sourceStatus.textContent = "Bu klasörde eşleşen *_girdi.json / *_etiket.json çifti bulunamadı.";
  }
});

function renderSplitTabs() {
  splitTabsEl.innerHTML = Object.entries(splits).map(([ad, s]) =>
    `<button class="split-tab" data-split="${ad}">${ad}<span class="n">${s.records.size} kayıt</span></button>`
  ).join("");
  splitTabsEl.querySelectorAll(".split-tab").forEach((btn) =>
    btn.addEventListener("click", () => setActiveSplit(btn.dataset.split))
  );
}

function setActiveSplit(splitAdi) {
  activeSplit = splitAdi;
  activeAnomaliFilter.clear();
  selectedKayitId = null;
  splitTabsEl.querySelectorAll(".split-tab").forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.split === splitAdi)
  );
  searchInput.value = "";
  filtrePaneliAcik = false;
  filterToggle.classList.remove("open");
  filterOptionsEl.classList.remove("open");
  renderResultList([]);
  answerEl.innerHTML = `<p class="empty-hint">Sonuçlardan bir kayıt seçin.</p>`;
}

// --- Arama ----------------------------------------------------------------

searchInput.addEventListener("input", () => {
  activeAnomaliFilter.clear();
  filterOptionsEl.querySelectorAll(".filter-option input").forEach((cb) => (cb.checked = false));
  const q = searchInput.value.trim();
  if (!q) { renderResultList([]); return; }

  const split = splits[activeSplit];
  const idAdayi = kayitIdNormalize(q);
  let eslesenler;
  if (idAdayi && split.records.has(idAdayi)) {
    eslesenler = [idAdayi];
  } else {
    const ql = q.toLocaleLowerCase("tr");
    eslesenler = Array.from(split.records.values())
      .filter((r) => r.saticiUnvan.toLocaleLowerCase("tr").includes(ql))
      .map((r) => r.kayitId);
  }
  renderResultList(eslesenler);
});

// --- Anomali türü filtresi (çoklu seçim) ------------------------------------

let filtrePaneliAcik = false;

filterToggle.addEventListener("click", () => {
  filtrePaneliAcik = !filtrePaneliAcik;
  if (filtrePaneliAcik) {
    const split = splits[activeSplit];
    const turler = Array.from(split.turSayaci.entries()).sort((a, b) => b[1] - a[1]);
    filterOptionsEl.innerHTML =
      `<div class="filter-option filter-clear" id="filter-clear-btn">Filtreyi kaldır</div>` +
      turler.map(([tur, adet]) => `
        <label class="filter-option">
          <span><input type="checkbox" data-tur="${tur}" ${activeAnomaliFilter.has(tur) ? "checked" : ""}> ${tur}</span>
          <span class="cnt">${adet}</span>
        </label>
      `).join("");

    filterOptionsEl.querySelectorAll("input[type=checkbox]").forEach((cb) =>
      cb.addEventListener("change", () => {
        if (cb.checked) activeAnomaliFilter.add(cb.dataset.tur);
        else activeAnomaliFilter.delete(cb.dataset.tur);
        searchInput.value = "";
        uygulaAnomaliFiltresi();
      })
    );
    document.getElementById("filter-clear-btn").addEventListener("click", () => {
      activeAnomaliFilter.clear();
      filterOptionsEl.querySelectorAll("input[type=checkbox]").forEach((cb) => (cb.checked = false));
      uygulaAnomaliFiltresi();
    });
  }
  filterToggle.classList.toggle("open", filtrePaneliAcik);
  filterOptionsEl.classList.toggle("open", filtrePaneliAcik);
});

function uygulaAnomaliFiltresi() {
  if (activeAnomaliFilter.size === 0) { renderResultList([]); return; }
  const split = splits[activeSplit];
  const eslesenler = Array.from(split.records.values())
    .filter((r) => r.anomaliTurleri.some((t) => activeAnomaliFilter.has(t)))
    .map((r) => r.kayitId);
  renderResultList(eslesenler);
}

// --- Sonuç listesi ----------------------------------------------------------

function renderResultList(kayitIdler) {
  if (!kayitIdler.length) {
    resultListEl.innerHTML = `<p class="empty-hint">Ara ya da filtrele.</p>`;
    return;
  }
  const split = splits[activeSplit];
  resultListEl.innerHTML = kayitIdler.map((kid) => {
    const r = split.records.get(kid);
    const grup = split.pairGroups.get(r.pairKey);
    const esVar = grup && grup.length > 1;
    return `<div class="result-item" data-kayit-id="${kid}">
      <span class="id">${kid} ${esVar ? '<span class="pair-tag">eş kayıt var</span>' : ""}</span>
      <span class="meta">${escapeHtml(r.saticiUnvan)} · ${r.genelToplam.toLocaleString("tr-TR", {minimumFractionDigits: 2})} ₺</span>
    </div>`;
  }).join("");
  resultListEl.querySelectorAll(".result-item").forEach((el) =>
    el.addEventListener("click", () => selectRecord(el.dataset.kayitId))
  );
}

// --- Kayıt seçimi + doğrulama -----------------------------------------------

async function selectRecord(kayitId) {
  selectedKayitId = kayitId;
  resultListEl.querySelectorAll(".result-item").forEach((el) =>
    el.classList.toggle("selected", el.dataset.kayitId === kayitId)
  );

  const split = splits[activeSplit];
  const rec = split.records.get(kayitId);
  const grup = split.pairGroups.get(rec.pairKey);
  const esKayitId = grup && grup.length > 1 ? grup.find((k) => k !== kayitId) : null;
  const esRec = esKayitId ? split.records.get(esKayitId) : null;

  answerEl.innerHTML = `<p class="empty-hint">Kontrol ediliyor...</p>`;

  try {
    const response = await fetch("/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fatura: rec.data, es_fatura: esRec ? esRec.data : null }),
    });
    if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
    renderAnswer(await response.json());
  } catch (err) {
    answerEl.innerHTML = `<p class="empty-hint">Hata: ${escapeHtml(err.message)}</p>`;
  }
}

function renderAnswer(result) {
  const fonksiyon = new Set(result.anomaly_types);
  const etiket = new Set(result.ground_truth ? result.ground_truth.anomaly_types : []);
  const ortak = [...fonksiyon].filter((t) => etiket.has(t));
  const yalnizFonksiyon = [...fonksiyon].filter((t) => !etiket.has(t));
  const yalnizEtiket = [...etiket].filter((t) => !fonksiyon.has(t));
  const tutarli = yalnizFonksiyon.length === 0 && yalnizEtiket.length === 0;

  let html = `<div class="result-summary">
    <span class="status-badge ${result.is_anomaly ? "anomaly" : "clean"}">${result.is_anomaly ? "Anomali var" : "Temiz"}</span>
    <span class="status-badge ${tutarli ? "consistent" : "inconsistent"}">${tutarli ? "Tutarlı" : "Tutarsızlık"}</span>
  </div>`;

  if (!tutarli) {
    html += `<div class="inconsistency-note">Fonksiyonun tespit ettiği ile etiketteki gerçek anomali kümesi uyuşmuyor.</div>`;
    html += chipSection("Ortak", ortak, "muted");
    html += chipSection("Yalnız fonksiyonda", yalnizFonksiyon, "");
    html += chipSection("Yalnız etikette", yalnizEtiket, "warn");
  } else {
    html += chipSection("Anomali türleri", [...fonksiyon], "");
  }

  html += `<div class="result-section"><h3>Kural ihlali detayları</h3>`;
  html += result.violation_details.length
    ? `<ul class="detail-list">${result.violation_details.map((d) => `<li>${escapeHtml(d)}</li>`).join("")}</ul>`
    : `<p class="empty-hint">Yok</p>`;
  html += `</div>`;

  answerEl.innerHTML = html;
}

function chipSection(baslik, turler, stil) {
  let h = `<div class="result-section"><h3>${baslik}</h3>`;
  h += turler.length
    ? `<div class="chip-row">${turler.map((t) => `<span class="chip ${stil}">${escapeHtml(t)}</span>`).join("")}</div>`
    : `<p class="empty-hint">Yok</p>`;
  return h + `</div>`;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}
