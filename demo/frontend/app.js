/* global AmazonCognitoIdentity */
"use strict";

const cfg = window.MEALLOG_CONFIG;

const userPool = new AmazonCognitoIdentity.CognitoUserPool({
  UserPoolId: cfg.userPoolId,
  ClientId: cfg.userPoolClientId,
});

let idToken = null; // JWT used as the Authorization header for the API
let myUserId = null; // this user's Cognito sub (from the ID token)

// Decode the "sub" claim from a JWT without any external library.
function subFromJwt(jwt) {
  try {
    const payload = JSON.parse(atob(jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return payload.sub || null;
  } catch (e) {
    return null;
  }
}

// Add the seeded sample user to the target-user dropdown if config.js provides
// its sub (deploy.sh fills these in when seed_data.sh has run).
function initTargetUserOptions() {
  if (cfg.sampleUserSub && cfg.sampleUserName) {
    const opt = document.createElement("option");
    opt.value = cfg.sampleUserSub;
    opt.textContent = "サンプル (" + cfg.sampleUserName + ")";
    $("targetUser").appendChild(opt);
  }
}

// ---------------------------------------------------------------------------
// Small DOM helpers
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const show = (el) => el.classList.remove("hidden");
const hide = (el) => el.classList.add("hidden");

function setAuthMsg(text, isError = false) {
  const el = $("authMsg");
  el.textContent = text;
  el.className = "msg " + (isError ? "error" : "ok");
}
function setMealMsg(text, isError = false) {
  const el = $("mealMsg");
  el.textContent = text;
  el.className = "msg " + (isError ? "error" : "ok");
}

// ---------------------------------------------------------------------------
// Fake credential generation (no real personal data is ever entered)
// username  : mealfan + random digits   -> also used as the display name
// email     : <username>@example.com
// password  : meets the Cognito policy (upper/lower/number, >= 8 chars)
// ---------------------------------------------------------------------------
function generateCredentials() {
  const n = Math.floor(1000 + Math.random() * 9000);
  const username = `mealfan${n}`;
  const email = `${username}@example.com`;
  const password = `Meal${n}log!`; // Aa + digits, satisfies the password policy
  return { username, email, password };
}

// ---------------------------------------------------------------------------
// Auth tab switching
// ---------------------------------------------------------------------------
$("tabSignIn").addEventListener("click", () => {
  $("tabSignIn").classList.add("active");
  $("tabSignUp").classList.remove("active");
  show($("signInForm"));
  hide($("signUpForm"));
  setAuthMsg("");
});
$("tabSignUp").addEventListener("click", () => {
  $("tabSignUp").classList.add("active");
  $("tabSignIn").classList.remove("active");
  hide($("signInForm"));
  show($("signUpForm"));
  setAuthMsg("");
});

// ---------------------------------------------------------------------------
// Sign up: create a fake, self-confirmed demo account
// ---------------------------------------------------------------------------
$("signUpForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const { username, email, password } = generateCredentials();

  const attributes = [
    new AmazonCognitoIdentity.CognitoUserAttribute({ Name: "email", Value: email }),
    new AmazonCognitoIdentity.CognitoUserAttribute({ Name: "nickname", Value: username }),
  ];

  setAuthMsg("アカウントを作成しています...");
  userPool.signUp(username, password, attributes, null, (err) => {
    if (err) {
      setAuthMsg("作成に失敗しました: " + (err.message || err), true);
      return;
    }
    // Email is auto-verified by the pool, so the user is confirmed/activated
    // automatically and the PostConfirmation Lambda fires. Show the login info.
    $("genUsername").textContent = username;
    $("genEmail").textContent = email;
    $("genPassword").textContent = password;
    show($("credentialBox"));
    setAuthMsg("アカウントを作成しました。下のログイン情報でサインインしてください。");

    // Pre-fill the sign-in form for convenience.
    $("siUsername").value = username;
    $("siPassword").value = password;
  });
});

$("goSignInBtn").addEventListener("click", () => {
  $("tabSignIn").click();
});

// ---------------------------------------------------------------------------
// Sign in
// ---------------------------------------------------------------------------
$("signInForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const username = $("siUsername").value.trim();
  const password = $("siPassword").value;

  const authDetails = new AmazonCognitoIdentity.AuthenticationDetails({
    Username: username,
    Password: password,
  });
  const cognitoUser = new AmazonCognitoIdentity.CognitoUser({
    Username: username,
    Pool: userPool,
  });

  setAuthMsg("サインインしています...");
  cognitoUser.authenticateUser(authDetails, {
    onSuccess: (session) => {
      idToken = session.getIdToken().getJwtToken();
      myUserId = subFromJwt(idToken);
      enterApp(username);
    },
    onFailure: (err) => {
      setAuthMsg("サインインに失敗しました: " + (err.message || err), true);
    },
  });
});

// ---------------------------------------------------------------------------
// Session / sign out
// ---------------------------------------------------------------------------
function enterApp(displayName) {
  $("whoami").textContent = `👤 ${displayName}`;
  show($("userBar"));
  hide($("authView"));
  show($("appView"));
  setAuthMsg("");
  $("mealDate").value = new Date().toISOString().slice(0, 10);
  loadMeals("recent");
}

$("signOutBtn").addEventListener("click", () => {
  const user = userPool.getCurrentUser();
  if (user) user.signOut();
  idToken = null;
  hide($("userBar"));
  hide($("appView"));
  show($("authView"));
  $("tabSignIn").click();
});

function restoreSession() {
  const user = userPool.getCurrentUser();
  if (!user) return;
  user.getSession((err, session) => {
    if (err || !session.isValid()) return;
    idToken = session.getIdToken().getJwtToken();
    myUserId = subFromJwt(idToken);
    enterApp(user.getUsername());
  });
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------
async function apiFetch(path, options = {}) {
  const res = await fetch(cfg.apiBaseUrl + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: idToken,
      ...(options.headers || {}),
    },
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

// Save a meal
$("mealForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    date: $("mealDate").value,
    slot: $("mealSlot").value,
    menu: $("mealMenu").value.trim(),
    rating: parseInt($("mealRating").value, 10),
    favorite: $("mealFavorite").checked,
  };
  try {
    setMealMsg("保存中...");
    await apiFetch("/meals", { method: "POST", body: JSON.stringify(body) });
    setMealMsg("保存しました。");
    $("mealMenu").value = "";
    $("mealFavorite").checked = false;
    loadMeals(currentMode);
  } catch (err) {
    setMealMsg("保存に失敗しました: " + err.message, true);
  }
});

// Filters: switching the active mode shows the matching controls.
let currentMode = "recent";
document.querySelectorAll(".filter").forEach((btn) => {
  if (!btn.dataset.mode) return;
  btn.addEventListener("click", () => {
    document.querySelectorAll(".filter").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const mode = btn.dataset.mode;
    $("recentControls").classList.toggle("hidden", mode !== "recent");
    $("ratingControls").classList.toggle("hidden", mode !== "rating");
    $("favoriteControls").classList.toggle("hidden", mode !== "favorite");
    loadMeals(mode);
  });
});

// Build the query string for the current mode from its controls.
function buildQuery(mode) {
  const params = new URLSearchParams();
  const targetUserId = $("targetUser").value; // "" = self
  if (targetUserId) params.set("userId", targetUserId);

  if (mode === "rating") {
    const rmode = $("ratingMode").value;
    const a = $("ratingA").value;
    const b = $("ratingB").value;
    if (rmode === "exact") params.set("rating", a);
    else if (rmode === "gte") params.set("ratingFrom", a);
    else if (rmode === "lte") params.set("ratingTo", a);
    else { // between
      const lo = Math.min(+a, +b), hi = Math.max(+a, +b);
      params.set("ratingFrom", lo);
      params.set("ratingTo", hi);
    }
  } else if (mode === "favorite") {
    // Favorites are global (no userId), only a date range applies.
    params.delete("userId");
    if ($("favDateFrom").value) params.set("dateFrom", $("favDateFrom").value);
    if ($("favDateTo").value) params.set("dateTo", $("favDateTo").value);
  } else if (mode === "recent") {
    const single = $("dateSingle").value;
    if (single) {
      params.set("date", single);
      params.set("slotFrom", $("slotFrom").value);
      params.set("slotTo", $("slotTo").value);
    } else {
      if ($("dateFrom").value) params.set("dateFrom", $("dateFrom").value);
      if ($("dateTo").value) params.set("dateTo", $("dateTo").value);
    }
  }
  const qs = params.toString();
  return qs ? "?" + qs : "";
}

async function loadMeals(mode) {
  currentMode = mode;
  let path;
  if (mode === "rating") {
    // rating needs at least an exact/range value; buildQuery supplies it.
    path = "/meals/search/rating" + buildQuery("rating");
  } else if (mode === "favorite") {
    path = "/meals/favorites" + buildQuery("favorite"); // global, optional date range
  } else {
    path = "/meals" + buildQuery("recent");
  }
  try {
    $("listInfo").textContent = "読み込み中...";
    const data = await apiFetch(path);
    renderMeals(data);
  } catch (err) {
    $("listInfo").textContent = "取得に失敗しました: " + err.message;
    $("mealsBody").innerHTML = "";
  }
}

// Recent (base table) controls
$("recentSearch").addEventListener("click", () => loadMeals("recent"));
$("recentReset").addEventListener("click", () => {
  $("dateFrom").value = "";
  $("dateTo").value = "";
  $("dateSingle").value = "";
  $("slotFrom").value = "1";
  $("slotTo").value = "3";
  loadMeals("recent");
});

// Rating (LSI) controls
$("ratingMode").addEventListener("change", () => {
  // Show the second rating selector only for "between".
  $("ratingBWrap").classList.toggle("hidden", $("ratingMode").value !== "between");
});
$("ratingSearch").addEventListener("click", () => loadMeals("rating"));

// Favorite (GSI) controls
$("favoriteSearch").addEventListener("click", () => loadMeals("favorite"));
$("favoriteReset").addEventListener("click", () => {
  $("favDateFrom").value = "";
  $("favDateTo").value = "";
  loadMeals("favorite");
});

$("targetUser").addEventListener("change", () => {
  // Favorites are global, so switching the target user only affects
  // recent / rating. Reload whichever of those is active.
  if (currentMode === "recent" || currentMode === "rating") loadMeals(currentMode);
});

const SLOT_LABEL = { breakfast: "朝", lunch: "昼", dinner: "夜" };

function renderMeals(data) {
  const modeLabel = {
    recent: "ベーステーブルを UserId + ソートキー範囲で Query",
    rating: "LSI(RatingIndex) を評価の範囲で Query",
    favorite: "GSI(FavoriteIndex / スパース) を Query",
  }[data.mode];
  const range = data.range ? `（条件: ${data.range}）` : "";
  $("listInfo").textContent = `${modeLabel}${range} — ${data.count} 件`;

  // 画像 URL を引くときの対象ユーザー（自分 or 選択中のサンプル）。
  const ownerUserId = data.userId || myUserId;

  const rows = (data.items || [])
    .map((it) => {
      const stars = "★".repeat(it.Rating) + "☆".repeat(5 - it.Rating);
      const fav = it.Favorite ? "⭐" : "";
      const owner = escapeHtml(it.Owner || "");
      const keyEnc = encodeURIComponent(it.MealKey);
      // 自分の記録だけ削除・写真アップロードが可能。
      const mine = it.UserId === myUserId;
      const del = mine
        ? `<button class="link-btn del" data-key="${keyEnc}">削除</button>`
        : "";
      // 写真セル: 画像があればサムネイル（ロード後に差し込む）、
      // 自分の記録なら「写真追加」ボタンを出す。
      const thumbCell = `<span class="thumb-slot" data-key="${keyEnc}" data-user="${encodeURIComponent(it.UserId)}"></span>` +
        (mine ? `<button class="link-btn photo" data-key="${keyEnc}">写真</button>` : "");
      return `<tr>
        <td class="thumb-cell">${thumbCell}</td>
        <td>${it.MealDate || ""}</td>
        <td>${SLOT_LABEL[it.Slot] || it.Slot || ""}</td>
        <td>${escapeHtml(it.Menu || "")}</td>
        <td>${stars}</td>
        <td>${fav}</td>
        <td>${owner}</td>
        <td>${del}</td>
      </tr>`;
    })
    .join("");
  $("mealsBody").innerHTML = rows || `<tr><td colspan="8" class="empty">記録がありません</td></tr>`;

  // 削除
  document.querySelectorAll(".del").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await apiFetch("/meals/" + btn.dataset.key, { method: "DELETE" });
        loadMeals(currentMode);
      } catch (err) {
        setMealMsg("削除に失敗しました: " + err.message, true);
      }
    });
  });

  // 写真追加ボタン
  document.querySelectorAll(".photo").forEach((btn) => {
    btn.addEventListener("click", () => startPhotoUpload(decodeURIComponent(btn.dataset.key)));
  });

  // 各行のサムネイルを非同期で取得して差し込む。
  document.querySelectorAll(".thumb-slot").forEach((slot) => {
    const mealKey = decodeURIComponent(slot.dataset.key);
    const uid = decodeURIComponent(slot.dataset.user);
    loadThumb(slot, mealKey, uid);
  });
}

// 指定した記録の画像 URL を取得し、サムネイルがあれば表示する。
async function loadThumb(slot, mealKey, userId) {
  try {
    const q = userId && userId !== myUserId ? "?userId=" + encodeURIComponent(userId) : "";
    const data = await apiFetch("/meals/" + encodeURIComponent(mealKey) + "/image" + q);
    if (data.thumbUrl) {
      const img = document.createElement("img");
      img.src = data.thumbUrl;
      img.className = "thumb";
      img.alt = "料理写真";
      img.addEventListener("click", () => openPhotoModal(data.webUrl || data.thumbUrl, data.description));
      slot.innerHTML = "";
      slot.appendChild(img);
    }
  } catch (e) {
    // 画像が無い場合などは何もしない。
  }
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ---------------------------------------------------------------------------
// 画像アップロード（署名付き URL で S3 へ直接 PUT）
// ---------------------------------------------------------------------------
let uploadTargetMealKey = null;

function startPhotoUpload(mealKey) {
  uploadTargetMealKey = mealKey;
  $("photoInput").value = "";
  $("photoInput").click();
}

$("photoInput").addEventListener("change", async () => {
  const file = $("photoInput").files[0];
  if (!file || !uploadTargetMealKey) return;
  const ext = (file.name.split(".").pop() || "jpg").toLowerCase();
  try {
    setMealMsg("アップロード用 URL を取得中...");
    // 1) 署名付き PUT URL を取得
    const { uploadUrl, contentType } = await apiFetch(
      "/meals/" + encodeURIComponent(uploadTargetMealKey) + "/image-url",
      { method: "POST", body: JSON.stringify({ ext }) }
    );
    // 2) S3 へ直接 PUT（API Gateway は経由しない）
    setMealMsg("S3 にアップロード中...");
    const putRes = await fetch(uploadUrl, {
      method: "PUT",
      headers: { "Content-Type": contentType },
      body: file,
    });
    if (!putRes.ok) throw new Error("S3 PUT に失敗 (" + putRes.status + ")");
    setMealMsg("アップロード完了。画像処理と説明生成は数十秒ほどで反映されます。");
    // 少し待ってから一覧を更新（処理完了を拾いやすくする）
    setTimeout(() => loadMeals(currentMode), 8000);
  } catch (err) {
    setMealMsg("アップロードに失敗しました: " + err.message, true);
  } finally {
    uploadTargetMealKey = null;
  }
});

// ---------------------------------------------------------------------------
// 写真ポップアップ
// ---------------------------------------------------------------------------
function openPhotoModal(url, description) {
  $("modalImage").src = url;
  $("modalDesc").textContent = description || "（説明は生成中かもしれません）";
  $("photoModal").classList.remove("hidden");
}
function closePhotoModal() {
  $("photoModal").classList.add("hidden");
  $("modalImage").src = "";
}
$("modalClose").addEventListener("click", closePhotoModal);
$("modalBackdrop").addEventListener("click", closePhotoModal);

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
initTargetUserOptions();
restoreSession();
