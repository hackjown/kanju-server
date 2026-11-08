(function bootstrapKanjuPage() {
  document.documentElement.dataset.theme = "movie-dark";
  document.documentElement.classList.add("dark");

  // 1. 初始化成年人确认标记、会话同步与清理广告缓存
  try {
    if (!localStorage.getItem("movie-search:adult-catalog-confirmed")) {
      localStorage.setItem("movie-search:adult-catalog-confirmed", "true");
    }
    if (!localStorage.getItem("movie-search:midnight-player-tutorial-seen")) {
      localStorage.setItem("movie-search:midnight-player-tutorial-seen", "true");
    }

    // 检查是否有登录凭证，确保前端 localStorage 状态即时同步
    if (document.cookie.includes("ai_movie_session") || document.cookie.includes("kj_sid")) {
      localStorage.setItem("movie-search:account-signed-in", "1");
      if (!localStorage.getItem("movie-search:account-scope")) {
        localStorage.setItem("movie-search:account-scope", "usr_1");
      }
    }

    // 清除历史缓存中的广告与翻转推荐配置
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const k = localStorage.key(i);
      if (k && (k.includes("app-experience") || k.includes("player-info-ad") || k.includes("startup-ad"))) {
        localStorage.removeItem(k);
      }
    }
  } catch (e) {
    console.warn("Bootstrap storage initialization failed:", e);
  }
})();
