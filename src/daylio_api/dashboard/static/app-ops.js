/* Daylio operations UI (admin + import). Loaded dynamically by app.js. */
(function () {
  "use strict";

  function $(id) {
    return document.getElementById(id);
  }

  function show(element) {
    if (element) element.classList.remove("hidden");
  }

  function hide(element) {
    if (element) element.classList.add("hidden");
  }

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

  var currentUser = window.__daylioCurrentUser || null;

  function loadUsers() {
    var tbody = $("users-tbody");
    var status = $("users-status");
    if (!tbody) return;
    tbody.innerHTML = "";
    if (status) status.textContent = "Loading...";
    request("/api/admin/users").then(function (result) {
      if (!result.ok) {
        if (status) status.textContent = result.data.error || "Failed to load users";
        return;
      }
      if (status) status.textContent = "";
      (result.data.users || []).forEach(function (user) {
        var tr = document.createElement("tr");
        var tdUser = document.createElement("td");
        tdUser.textContent = user.username;
        var tdAdmin = document.createElement("td");
        tdAdmin.textContent = user.is_admin ? "Yes" : "No";
        var tdDir = document.createElement("td");
        tdDir.textContent = user.import_dir || "-";
        var tdCount = document.createElement("td");
        tdCount.textContent = user.records || 0;
        var tdActions = document.createElement("td");
        tdActions.className = "actions";
        var admins = (result.data.users || []).filter(function (u) { return u.is_admin; });
        if (!(user.is_admin && admins.length <= 1)) {
          var delBtn = document.createElement("button");
          delBtn.type = "button";
          delBtn.className = "danger small";
          delBtn.textContent = "Delete";
          delBtn.onclick = function () {
            if (!confirm("Delete user " + user.username + "? This will remove all their data.")) return;
            request("/api/admin/users/" + encodeURIComponent(user.username), { method: "DELETE" }).then(function (res) {
              if (!res.ok) {
                alert(res.data.error || "Failed to delete user");
                return;
              }
              loadUsers();
            });
          };
          tdActions.appendChild(delBtn);
        }
        tr.appendChild(tdUser);
        tr.appendChild(tdAdmin);
        tr.appendChild(tdDir);
        tr.appendChild(tdCount);
        tr.appendChild(tdActions);
        tbody.appendChild(tr);
      });
    });
  }

  function setup() {
    currentUser = window.__daylioCurrentUser || currentUser;

    // Upload form
    var uploadForm = $("upload-form");
    if (uploadForm && !uploadForm.__opsBound) {
      uploadForm.__opsBound = true;
      uploadForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var fileInput = $("upload-file");
        var status = $("upload-status");
        var resultEl = $("upload-result");
        var file = fileInput.files[0];
        if (!file) {
          if (status) status.textContent = "Please select a file";
          return;
        }
        var formData = new FormData();
        formData.append("file", file);
        if (currentUser && currentUser.is_admin && $("upload-user")) {
          formData.append("user", $("upload-user").value);
        }
        if (status) status.textContent = "Uploading...";
        if (resultEl) { hide(resultEl); resultEl.textContent = ""; }
        fetch("/api/import", {
          method: "POST",
          body: formData,
          credentials: "same-origin"
        }).then(function (res) {
          return res.json().then(function (data) {
            return { ok: res.ok, data: data };
          });
        }).then(function (res) {
          if (!res.ok) {
            if (status) status.textContent = res.data.error || "Upload failed";
            return;
          }
          if (status) status.textContent = "Import completed";
          if (resultEl) {
            resultEl.textContent = JSON.stringify(res.data.job, null, 2);
            show(resultEl);
          }
        }).catch(function (err) {
          if (status) status.textContent = "Error: " + err.message;
        });
      });
    }

    // Folder import form
    var folderForm = $("folder-import-form");
    if (folderForm && !folderForm.__opsBound) {
      folderForm.__opsBound = true;
      folderForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var status = $("folder-status");
        var resultEl = $("folder-result");
        if (status) status.textContent = "Scanning folder...";
        if (resultEl) { hide(resultEl); resultEl.textContent = ""; }
        var body = {};
        if (currentUser && currentUser.is_admin && $("folder-user")) {
          body.user = $("folder-user").value;
        }
        request("/api/import/folder", { method: "POST", body: Object.keys(body).length ? body : undefined }).then(function (res) {
          if (!res.ok) {
            if (status) status.textContent = res.data.error || "Import failed";
            return;
          }
          if (status) status.textContent = "Import completed";
          if (resultEl) {
            resultEl.textContent = JSON.stringify(res.data.imports, null, 2);
            show(resultEl);
          }
        });
      });
    }

    // Create user form
    var createUserForm = $("create-user-form");
    if (createUserForm && !createUserForm.__opsBound) {
      createUserForm.__opsBound = true;
      createUserForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var status = $("create-user-status");
        if (status) status.textContent = "Creating...";
        request("/api/admin/users", {
          method: "POST",
          body: {
            username: $("new-username").value,
            password: $("new-password").value,
            import_dir: $("new-import-dir").value || null,
            is_admin: $("new-is-admin").checked
          }
        }).then(function (res) {
          if (!res.ok) {
            if (status) status.textContent = res.data.error || "Failed to create user";
            return;
          }
          if (status) status.textContent = "User created";
          $("new-username").value = "";
          $("new-password").value = "";
          $("new-import-dir").value = "";
          $("new-is-admin").checked = false;
          loadUsers();
        });
      });
    }

    // Setup selects for admin
    if (currentUser && currentUser.is_admin) {
      var uploadUserField = $("upload-user-field");
      var uploadUserSelect = $("upload-user");
      var folderUserField = $("folder-user-field");
      var folderUserSelect = $("folder-user");
      request("/api/users").then(function (result) {
        if (!result.ok) return;
        var users = result.data.users || [];
        if (uploadUserSelect) {
          uploadUserSelect.innerHTML = "";
          users.forEach(function (item) {
            var opt = document.createElement("option");
            opt.value = item.username;
            opt.textContent = item.username;
            uploadUserSelect.appendChild(opt);
          });
          uploadUserSelect.value = currentUser.username;
          if (uploadUserField) show(uploadUserField);
        }
        if (folderUserSelect) {
          folderUserSelect.innerHTML = "";
          users.forEach(function (item) {
            var opt = document.createElement("option");
            opt.value = item.username;
            opt.textContent = item.username;
            folderUserSelect.appendChild(opt);
          });
          folderUserSelect.value = currentUser.username;
          if (folderUserField) show(folderUserField);
        }
      });
      loadUsers();
    }
  }

  window.__daylioOps = { loadUsers: loadUsers, setup: setup };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setup);
  } else {
    setup();
  }
})();
