// src/lib/api.ts
export const apiFetch = async (endpoint: string, options: RequestInit = {}): Promise<any> => {
  // Helper to always get the freshest token
  const getAuthHeader = () => {
    const token = localStorage.getItem("access_token") || "";
    return token ? { Authorization: `Bearer ${token}` } : {};
  };

  let headers = {
    "Content-Type": "application/json",
    ...getAuthHeader(),
    ...options.headers,
  };

  // Use the defined base URL (prefer env var)
  const base = import.meta.env.VITE_API_BASE_URL || "http://localhost:5000"; // ← Changed default to localhost

  let response = await fetch(`${base}${endpoint}`, {  // ← Use full URL (not proxy /api)
    ...options,
    headers,
    credentials: "include",  // Added: for cookies/sessions if backend uses them
  });

  // Auto-refresh on 401 (good logic, but add check for refresh endpoint)
  if (response.status === 401) {
    const refreshToken = localStorage.getItem("refresh_token");

    if (!refreshToken) {
      console.warn("No refresh token → logging out");
      localStorage.clear();
      window.location.href = "/login";
      throw new Error("Session expired - no refresh token");
    }

    const refreshRes = await fetch(`${base}/api/auth/refresh`, {  // ← Use base here too
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${refreshToken}`,
      },
      credentials: "include",
    });

    if (!refreshRes.ok) {
      console.warn("Refresh failed → logging out");
      localStorage.clear();
      window.location.href = "/login";
      throw new Error("Refresh token invalid");
    }

    const { access_token: newAccessToken } = await refreshRes.json();

    // Save new token
    localStorage.setItem("access_token", newAccessToken);

    // Rebuild headers with fresh token
    headers = {
      "Content-Type": "application/json",
      ...getAuthHeader(),  // now uses new token
      ...options.headers,
    };

    // Retry original request
    response = await fetch(`${base}${endpoint}`, {
      ...options,
      headers,
      credentials: "include",
    });
  }

  if (!response.ok) {
    let errData;
    try {
      errData = await response.json();
    } catch {
      errData = {};
    }

    const errorMessage =
      errData.error ||
      errData.msg ||
      (response.status === 401 ? "Session expired - please log in again" :
       response.status === 403 ? "Permission denied" :
       response.status === 404 ? "Resource not found" :
       `Request failed (${response.status})`);

    throw new Error(errorMessage);
  }

  return response.json();
};

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:5000";
export const SOCKET_URL = import.meta.env.VITE_SOCKET_URL || "http://localhost:5000";