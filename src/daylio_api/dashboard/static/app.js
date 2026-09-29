/* Daylio dashboard SPA logic (Fase 6, WORKPLAN §5.7 / R4).
 *
 * =============================== MAINTENANCE NOTES ===============================
 * Module   : src/daylio_api/dashboard/static/app.js
 * Scope    : vanilla ES2018 SPA, no bundler and no framework. Bootstraps the
 *            session (GET /api/auth/me -> login form on 401), then renders five
 *            Chart.js charts from /api/analytics/*, which read InfluxDB ONLY
 *            (R4/D4). A 503 renders the "InfluxDB not available" banner and the
 *            charts are left empty: the SPA never falls back to SQL.
 *
 * Endpoints (all protected, D6 user filter + inclusive date range):
 *   GET /api/auth/me                                        -> {user} | 401
 *   POST /api/auth/login   {username, password}             -> {user} | 401
 *   POST /api/auth/logout                                   -> {ok}
 *   GET /api/users                                          -> {users}   (admin)
 *   GET /api/analytics/mood-over-time                       -> {dates, values, levels}
 *   GET /api/analytics/mood-distribution                    -> {levels, labels, counts}
 *   GET /api/analytics/heatmap                              -> {weekdays, buckets, data, counts}
 *   GET /api/analytics/scales-trend[?scale=]                -> {scales:[{dates, values, labels}]}
 *   GET /api/analytics/activities-top                       -> {activities:[{activity, count}]}
 *
 * Chart.js: v4.5.1 vendored at vendor/chart.umd.min.js (no CDN at runtime,
 * D13). Charts are destroyed before re-render so a refresh never leaks
 * instances. The heatmap is rendered as a minimal HTML matrix on purpose: a
 * matrix Chart.js plugin would mean vendoring a second library for one chart.
 * ================================================================================
 */
(function () {
  "use strict";

  /** Charts currently mounted, keyed by canvas id. */
  var charts = {};

  /** Latest resolved filter scope (shared by every analytics call). */
  var scope = { user: null, start: null, end: null, granularity: "auto" };

  /* ------------------------------------------------------------------ */
  /* Tiny DOM helpers                                                     */
  /* ------------------------------------------------------------------ */
  function $(id) {
    return document.getElementById(id);
  }

  function show(element) {
    if (element) element.classList.remove("hidden");
  }

  function hide(element) {
    if (element) element.classList.add("hidden");
  }

  function setBanner(message, kind) {
    var banner = $("status-banner");
    if (!banner) return;
    if (!message) {
      hide(banner);
      banner.textContent = "";
      return;
    }
    banner.textContent = message;
    banner.className = "banner" + (kind ? " banner-" + kind : "");
  }

  function setInfluxStatus(ok) {
    var badge = $("influx-status");
    if (!badge) return;
    badge.textContent = ok ? "InfluxDB: OK" : "InfluxDB: unavailable";
    badge.className = "badge " + (ok ? "badge-ok" : "badge-error");
  }

  /* ------------------------------------------------------------------ */
  /* Networking                                                           */
  /* ------------------------------------------------------------------ */
  function request(path, options) {
    var opts = options || {};
    return fetch(path, {
      method: opts.method || "GET",
      headers: opts.body ? { "Content-Type": "application/json" } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      credentials: "same-origin"
    }).then(function (response) {
      return response
        .json()
        .catch(function () {
          return {};
        })
        .then(function (data) {
          return { ok: response.ok, status: response.status, data: data };
        });
    });
  }

  /** Error message for a failed analytics call, with the 503 case spelled out. */
  function describeError(result, chart) {
    if (result.status === 503) {
      setInfluxStatus(false);
      return "InfluxDB not available — analytics are Influx-only (Influx non disponibile): "
        + (result.data.error || "unreachable") + " [" + chart + "]";
    }
    if (result.status === 401) {
      return "Session expired, please sign in again. [" + chart + "]";
    }
    if (result.status === 403) {
      return "You are not allowed to read this user's analytics. [" + chart + "]";
    }
    return (result.data.error || "request failed") + " [" + chart + "]";
  }

  /* ------------------------------------------------------------------ */
  /* Filter scope                                                          */
  /* ------------------------------------------------------------------ */
  function isoDate(date) {
    return date.toISOString().slice(0, 10);
  }

  function applyQuickRange(days) {
    var today = new Date();
    var start;
    if (days === "all") {
      start = null;
    } else {
      start = new Date(today.getTime() - (parseInt(days, 10) - 1) * 86400000);
    }
    $("start-date").value = start ? isoDate(start) : "";
    $("end-date").value = isoDate(today);
  }

  function readScope() {
    scope.start = $("start-date").value || null;
    scope.end = $("end-date").value || null;
    scope.granularity = $("granularity").value || "auto";
    var select = $("user-select");
    scope.user = select && select.value ? select.value : null;
  }

  /** Query string shared by every analytics call (null values are dropped). */
  function query(extra) {
    var params = new URLSearchParams();
    if (scope.user) params.set("user", scope.user);
    if (scope.start) params.set("start_date", scope.start);
    if (scope.end) params.set("end_date", scope.end);
    Object.keys(extra || {}).forEach(function (key) {
      if (extra[key] !== null && extra[key] !== undefined) params.set(key, extra[key]);
    });
    var encoded = params.toString();
    return encoded ? "?" + encoded : "";
  }

  /* ------------------------------------------------------------------ */
  /* Chart.js rendering                                                   */
  /* ------------------------------------------------------------------ */
  function hasChartJs() {
    return typeof window.Chart !== "undefined";
  }

  function mountChart(canvasId, config) {
    if (!hasChartJs()) {
      setBanner("Chart.js is not loaded: vendored file missing "
        + "(/dashboard/static/vendor/chart.umd.min.js).", "error");
      return null;
    }
    var canvas = $(canvasId);
    if (!canvas) return null;
    if (charts[canvasId]) {
      charts[canvasId].destroy();
      delete charts[canvasId];
    }
    charts[canvasId] = new window.Chart(canvas.getContext("2d"), config);
    return charts[canvasId];
  }

  /** Mood palette shared by the line, doughnut and heatmap charts. */
  var MOOD_COLORS = {
    1: "#2e7d32",
    2: "#7cb342",
    3: "#f9a825",
    4: "#ef6c00",
    5: "#c62828"
  };

  function renderMoodOverTime(data) {
    var hint = $("mood-over-time-hint");
    if (hint) {
      // Weekly buckets collapse several days into one point: report the
      // number of plotted points (data.buckets), not the days read.
      hint.textContent = data.granularity === "week"
        ? "(weekly averages · " + data.buckets + " buckets)"
        : "(daily averages · 1 = best, 5 = worst)";
    }
    mountChart("chart-mood-over-time", {
      type: "line",
      data: {
        labels: data.dates,
        datasets: [
          {
            label: "Average mood",
            data: data.values,
            spanGaps: true,
            tension: 0.25,
            borderColor: "#1b5e20",
            backgroundColor: "rgba(27,94,32,0.12)",
            fill: true,
            pointRadius: data.dates.length > 120 ? 0 : 2
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: { y: { min: 1, max: 5, title: { display: true, text: "mood" } } },
        plugins: { legend: { display: false } }
      }
    });
  }

  function renderMoodDistribution(data) {
    mountChart("chart-mood-distribution", {
      type: "doughnut",
      data: {
        labels: (data.labels || []).map(function (label, index) {
          return (label || "level " + data.levels[index]) + " (" + data.levels[index] + ")";
        }),
        datasets: [
          {
            data: data.counts,
            backgroundColor: data.levels.map(function (level) {
              return MOOD_COLORS[level] || "#90a4ae";
            })
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: "bottom" } }
      }
    });
  }

  function renderActivities(data) {
    var items = data.activities || [];
    mountChart("chart-activities-top", {
      type: "bar",
      data: {
        labels: items.map(function (item) {
          return item.activity;
        }),
        datasets: [
          {
            label: "days",
            data: items.map(function (item) {
              return item.count;
            }),
            backgroundColor: "#1565c0"
          }
        ]
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { beginAtZero: true } }
      }
    });
  }

  function renderScales(data) {
    var container = $("scales-trend-charts");
    if (!container) return;
    var hint = $("scales-trend-hint");
    // Destroy the previous per-scale charts before wiping the container.
    Object.keys(charts).forEach(function (key) {
      if (key.indexOf("chart-scale-") === 0) {
        charts[key].destroy();
        delete charts[key];
      }
    });
    container.innerHTML = "";
    var series = data.scales || [];
    if (hint) {
      hint.textContent = series.length
        ? "(" + series.length + " scale" + (series.length > 1 ? "s" : "") + ")"
        : "(no scale recorded in this range)";
    }
    if (!series.length || !hasChartJs()) return;

    series.forEach(function (item) {
      var wrapper = document.createElement("div");
      wrapper.className = "scale-box";
      var title = document.createElement("h3");
      title.textContent = item.scale_label + " — " + item.days_with_value + "/" + item.days
        + " days";
      var canvas = document.createElement("canvas");
      canvas.id = "chart-scale-" + item.scale_name;
      wrapper.appendChild(title);
      wrapper.appendChild(canvas);
      container.appendChild(wrapper);
      mountChart(canvas.id, {
        type: "line",
        data: {
          labels: item.dates,
          datasets: [
            {
              label: item.scale_label,
              // null for the days without the scale: the line shows a gap
              // instead of a fake 0 (see the endpoint contract).
              data: item.values,
              spanGaps: true,
              borderColor: "#6a1b9a",
              backgroundColor: "rgba(106,27,154,0.12)",
              pointRadius: item.dates.length > 120 ? 0 : 2
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } }
        }
      });
    });
  }

  function renderHeatmap(data) {
    var host = $("heatmap");
    var legend = $("heatmap-legend");
    if (!host) return;
    var metric = data.metric === "count" ? "records" : "average mood (1 = best)";
    var weekdays = data.weekdays || [];
    var buckets = data.buckets || [];
    var matrix = data.data || [];
    var counts = data.counts || [];

    var table = document.createElement("table");
    table.className = "heatmap";
    var head = document.createElement("thead");
    var headRow = document.createElement("tr");
    var corner = document.createElement("th");
    corner.textContent = metric;
    headRow.appendChild(corner);
    buckets.forEach(function (bucket) {
      var th = document.createElement("th");
      th.textContent = bucket;
      headRow.appendChild(th);
    });
    head.appendChild(headRow);
    table.appendChild(head);

    var body = document.createElement("tbody");
    weekdays.forEach(function (weekday, rowIndex) {
      var tr = document.createElement("tr");
      var th = document.createElement("th");
      th.textContent = weekday;
      tr.appendChild(th);
      (matrix[rowIndex] || []).forEach(function (value, columnIndex) {
        var td = document.createElement("td");
        var count = (counts[rowIndex] || [])[columnIndex] || 0;
        if (value === null || value === undefined) {
          td.className = "cell cell-empty";
          td.textContent = "";
        } else {
          // 1 = green (best) ... 5 = red (worst), reused for counts too.
          var level = data.metric === "count" ? 3 : Math.max(1, Math.min(5, Math.round(value)));
          td.className = "cell cell-" + level;
          td.textContent = data.metric === "count" ? String(value) : String(value);
          td.title = weekday + " " + buckets[columnIndex] + "h — " + count + " record(s)";
        }
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    table.appendChild(body);
    host.innerHTML = "";
    host.appendChild(table);
    if (legend) {
      legend.textContent = "cells: " + metric + " · " + (data.total || 0) + " record(s) in range";
    }
  }

  /* ------------------------------------------------------------------ */
  /* Data loading                                                         */
  /* ------------------------------------------------------------------ */
  function loadAnalytics() {
    readScope();
    setBanner("");
    var errors = [];
    var ok = 0;

    function fail(chart) {
      return function (result) {
        errors.push(describeError(result, chart));
        if (result.status === 503) setInfluxStatus(false);
        else setInfluxStatus(true);
      };
    }

    request("/api/analytics/mood-over-time" + query(
      scope.granularity === "auto" ? {} : { granularity: scope.granularity }
    ))
      .then(function (result) {
        if (!result.ok) return fail("mood-over-time")(result);
        ok += 1;
        renderMoodOverTime(result.data);
      })
      .catch(fail("mood-over-time"));

    request("/api/analytics/mood-distribution" + query())
      .then(function (result) {
        if (!result.ok) return fail("mood-distribution")(result);
        ok += 1;
        renderMoodDistribution(result.data);
      })
      .catch(fail("mood-distribution"));

    request("/api/analytics/activities-top" + query())
      .then(function (result) {
        if (!result.ok) return fail("activities-top")(result);
        ok += 1;
        renderActivities(result.data);
      })
      .catch(fail("activities-top"));

    request("/api/analytics/scales-trend" + query())
      .then(function (result) {
        if (!result.ok) return fail("scales-trend")(result);
        ok += 1;
        renderScales(result.data);
      })
      .catch(fail("scales-trend"));

    request("/api/analytics/heatmap" + query())
      .then(function (result) {
        if (!result.ok) return fail("heatmap")(result);
        ok += 1;
        renderHeatmap(result.data);
      })
      .catch(fail("heatmap"));

    window.setTimeout(function () {
      if (ok > 0) setInfluxStatus(true);
      if (errors.length) {
        setBanner(errors.join(" — "), errors.some(function (e) { return e.indexOf("InfluxDB") === 0; })
          ? "error"
          : "warn");
      }
    }, 0);
  }

  /* ------------------------------------------------------------------ */
  /* Session + boot                                                       */
  /* ------------------------------------------------------------------ */
  function showLogin() {
    hide($("dashboard-view"));
    show($("login-view"));
  }

  function showDashboard(user) {
    hide($("login-view"));
    show($("dashboard-view"));
    $("current-user").textContent = user.username + (user.is_admin ? " (admin)" : "");
    applyQuickRange($("quick-range").value);
    if (user.is_admin) {
      request("/api/users").then(function (result) {
        if (!result.ok) return;
        var select = $("user-select");
        select.innerHTML = "";
        (result.data.users || []).forEach(function (item) {
          var option = document.createElement("option");
          option.value = item.username;
          option.textContent = item.username;
          select.appendChild(option);
        });
        select.value = user.username;
        show($("user-field"));
        loadAnalytics();
      });
      return;
    }
    loadAnalytics();
  }

  function boot() {
    request("/api/auth/me").then(function (result) {
      if (result.status === 401) {
        showLogin();
        return;
      }
      if (!result.ok) {
        showLogin();
        setBanner((result.data.error || "session check failed"), "error");
        return;
      }
      showDashboard(result.data.user);
    });
  }

  /* ------------------------------------------------------------------ */
  /* Event wiring (registered once, at the bottom of the file)            */
  /* ------------------------------------------------------------------ */
  document.addEventListener("DOMContentLoaded", function () {
    $("login-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var error = $("login-error");
      hide(error);
      request("/api/auth/login", {
        method: "POST",
        body: { username: $("login-username").value, password: $("login-password").value }
      }).then(function (result) {
        if (!result.ok) {
          error.textContent = result.data.error || "invalid credentials";
          show(error);
          return;
        }
        showDashboard(result.data.user);
      });
    });

    $("logout-button").addEventListener("click", function () {
      request("/api/auth/logout", { method: "POST" }).then(function () {
        Object.keys(charts).forEach(function (key) {
          charts[key].destroy();
          delete charts[key];
        });
        setBanner("");
        setInfluxStatus(true);
        showLogin();
      });
    });

    $("filters-form").addEventListener("submit", function (event) {
      event.preventDefault();
      loadAnalytics();
    });

    $("quick-range").addEventListener("change", function () {
      applyQuickRange(this.value);
      loadAnalytics();
    });

    $("user-select").addEventListener("change", function () {
      loadAnalytics();
    });

    boot();
  });
})();
