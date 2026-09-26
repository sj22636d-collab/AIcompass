// 【変更】ログイン状態の管理と、API 呼び出しの共通処理
//
// バックエンドとの取り決め (バックエンドは未実装):
//   POST /api/register  {username, password} → 200 {token, username} / 409 名前が使用済み
//   POST /api/login     {username, password} → 200 {token, username} / 401 名前かパスワードが違う
//   POST /api/logout    (要ログイン)          → 200
//   それ以外の /api/*   は Authorization: Bearer <token> が必須。無効なら 401
//
// ログイン情報 (token と username) はブラウザの localStorage に保存する。

// 画面はバックエンドから配信されるので、API も同じサーバーの相対パスで呼ぶ
const API_BASE = "";
const AUTH_KEY = "auth";

function getAuth() {
    try {
        const auth = JSON.parse(localStorage.getItem(AUTH_KEY));
        return auth && auth.token && auth.username ? auth : null;
    } catch (e) {
        return null;
    }
}

function saveAuth(auth) {
    localStorage.setItem(AUTH_KEY, JSON.stringify({ token: auth.token, username: auth.username }));
}

function clearAuth() {
    try {
        localStorage.removeItem(AUTH_KEY);
    } catch (e) {}
}

// 未ログインならログイン画面へ移動する。各ページの最初に呼ぶ
function requireLogin() {
    const auth = getAuth();
    if (!auth) {
        location.replace('login.html');
        return null;
    }
    return auth;
}

// ログイン中のユーザーのトークンを付けて API を呼ぶ。
// トークンが無効 (401) ならログイン画面へ戻す
async function apiFetch(path, options = {}) {
    const auth = getAuth();
    const headers = { ...(options.headers || {}) };
    if (auth) headers["Authorization"] = `Bearer ${auth.token}`;

    const res = await fetch(API_BASE + path, { ...options, headers });
    if (res.status === 401 && auth) {
        clearAuth();
        location.replace('login.html');
    }
    return res;
}

async function logout() {
    try {
        await apiFetch("/api/logout", { method: "POST" });
    } catch (e) {
        // サーバーに届かなくても、この端末からはログアウトする
    }
    clearAuth();
    location.href = 'login.html';
}

// 今日の予定はユーザーごとに別のキーで保存する (同じ端末で別ユーザーの予定が見えないように)
function planStorageKey() {
    const auth = getAuth();
    return auth ? `todayPlan:${auth.username}` : 'todayPlan';
}
