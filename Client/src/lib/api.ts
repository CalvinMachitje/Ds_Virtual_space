// Client/src/lib/api.ts

export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || "http://localhost:5001";

export const SOCKET_URL =
  import.meta.env.VITE_SOCKET_URL || "http://localhost:5001";

/**
 * Central token handler (registered by AuthContext)
 */
let tokenHandler: ((data: {
  access_token: string;
  refresh_token?: string;
  user?: any;
}) => void) | null = null;

/**
 * AuthProvider registers this so apiFetch can update tokens globally
 */
export const registerTokenHandler = (
  handler: (data: {
    access_token: string;
    refresh_token?: string;
    user?: any;
  }) => void
) => {
  tokenHandler = handler;
};

/**
 * API Fetch wrapper
 */
export const apiFetch = async (
  endpoint: string,
  options: RequestInit = {}
): Promise<any> => {
  const getAuthHeader = () => {
    const token = localStorage.getItem("access_token") || "";
    return token ? { Authorization: `Bearer ${token}` } : {};
  };

  let headers = {
    "Content-Type": "application/json",
    ...getAuthHeader(),
    ...options.headers,
  };

  let response = await fetch(`${API_BASE_URL}${endpoint}`, {
    ...options,
    headers,
    credentials: "include",
  });

  /**
   * Automatic token refresh
   */
  if (response.status === 401) {
    const refreshToken = localStorage.getItem("refresh_token");

    if (!refreshToken) {
      console.warn("No refresh token → logging out");
      localStorage.clear();
      window.location.href = "/login";
      throw new Error("Session expired - no refresh token");
    }

    const refreshRes = await fetch(`${API_BASE_URL}/api/auth/refresh`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ refresh_token: refreshToken }),
      credentials: "include",
    });

    if (!refreshRes.ok) {
      console.warn("Refresh failed → logging out");
      localStorage.clear();
      window.location.href = "/login";
      throw new Error("Refresh token invalid");
    }

    const refreshData = await refreshRes.json();

    const newAccessToken = refreshData.access_token;
    const newRefreshToken = refreshData.refresh_token;

    /**
     * Update via centralized handler
     */
    if (tokenHandler) {
      tokenHandler({
        access_token: newAccessToken,
        refresh_token: newRefreshToken,
      });
    } else {
      localStorage.setItem("access_token", newAccessToken);
      if (newRefreshToken) {
        localStorage.setItem("refresh_token", newRefreshToken);
      }
    }

    /**
     * Retry original request
     */
    headers = {
      "Content-Type": "application/json",
      Authorization: `Bearer ${newAccessToken}`,
      ...options.headers,
    };

    response = await fetch(`${API_BASE_URL}${endpoint}`, {
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
      (response.status === 401
        ? "Session expired - please log in again"
        : response.status === 403
        ? "Permission denied"
        : response.status === 404
        ? "Resource not found"
        : `Request failed (${response.status})`);

    throw new Error(errorMessage);
  }

  return response.json();
};