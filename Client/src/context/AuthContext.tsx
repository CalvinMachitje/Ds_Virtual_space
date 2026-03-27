// Client/src/context/AuthContext.tsx
import React, {
  createContext,
  useContext,
  useEffect,
  useState,
  ReactNode,
} from "react";
import { toast } from "sonner";
import { API_BASE_URL, SOCKET_URL } from "@/lib/api";
import { io, Socket } from "socket.io-client";

export type UserRole = "buyer" | "seller" | "admin";

export interface User {
  id: string;
  email: string;
  full_name?: string;
  role: UserRole;
  admin_level?: string | null;
  [key: string]: any;
}

interface SignUpParams {
  email: string;
  password: string;
  full_name?: string;
  phone?: string | null;
  role?: "buyer" | "seller";
}

interface AuthContextType {
  session: { access_token: string } | null;
  user: User | null;
  loading: boolean;
  signUp: (params: SignUpParams) => Promise<{ error: Error | null }>;
  signIn: (email: string, password: string) => Promise<{ error: Error | null }>;
  adminLogin: (email: string, password: string) => Promise<{ error: Error | null }>;
  signOut: () => Promise<void>;
  userRole: UserRole | null;
  isAdmin: boolean;
  adminLevel?: string | null;
  socket: Socket | null;
  handleOAuthLogin: (data: { access_token: string; refresh_token?: string; user?: User }) => void;
  refreshAccessToken: () => Promise<string | null>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<{ access_token: string } | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [userRole, setUserRole] = useState<UserRole | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [adminLevel, setAdminLevel] = useState<string | null>(null);
  const [socket, setSocket] = useState<Socket | null>(null);

  /** --- OAuth login helper --- */
  const handleOAuthLogin = (data: { access_token: string; refresh_token?: string; user?: User }) => {
    localStorage.setItem("access_token", data.access_token);
    if (data.refresh_token) localStorage.setItem("refresh_token", data.refresh_token);
    setSession({ access_token: data.access_token });

    if (data.user) {
      setUser(data.user);
      setUserRole(data.user.role);
      setIsAdmin(data.user.role === "admin");
      setAdminLevel(data.user.admin_level || null);
    }
  };

  /** --- Safe JSON parse --- */
  const safeParse = async (res: Response) => {
    try {
      return await res.json();
    } catch {
      return null;
    }
  };

  /** --- Restore session from localStorage --- */
  useEffect(() => {
    const loadAuth = async () => {
      setLoading(true);
      try {
        const accessToken = localStorage.getItem("access_token");
        if (!accessToken) { setLoading(false); return; }

        const res = await fetch(`${API_BASE_URL}/api/auth/me`, {
          headers: { Authorization: `Bearer ${accessToken}` },
          credentials: "include",
        });

        const data = await safeParse(res);
        if (res.ok && data?.user) {
          setSession({ access_token: accessToken });
          setUser(data.user);
          setUserRole(data.user.role);
          setIsAdmin(data.user.role === "admin");
          setAdminLevel(data.user.admin_level || null);
        } else {
          localStorage.clear();
        }
      } catch (err) {
        console.error("Auth init error:", err);
        localStorage.clear();
      } finally { setLoading(false); }
    };
    loadAuth();
  }, []);

  /** --- Refresh access token --- */
  const refreshAccessToken = async (): Promise<string | null> => {
    try {
      const refreshToken = localStorage.getItem("refresh_token");
      if (!refreshToken) throw new Error("No refresh token available");

      const res = await fetch(`${API_BASE_URL}/api/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
        credentials: "include",
      });

      if (!res.ok) {
        const err = await safeParse(res);
        throw new Error(err?.error || `Refresh failed (${res.status})`);
      }

      const data = await safeParse(res);
      const newAccessToken = data?.access_token;
      if (!newAccessToken) throw new Error("No new access token returned");

      localStorage.setItem("access_token", newAccessToken);
      setSession({ access_token: newAccessToken });

      console.log("[Auth] Access token refreshed");
      toast.success("Session refreshed");
      return newAccessToken;
    } catch (err: any) {
      console.error("[Auth] Refresh failed:", err.message);
      toast.error("Session expired – please log in again");
      await signOut();
      return null;
    }
  };

  /** --- Socket.IO connection --- */
  useEffect(() => {
    if (!session?.access_token || !user?.id) {
      socket?.disconnect();
      setSocket(null);
      return;
    }

    if (socket?.connected) return;

    const newSocket = io("http://127.0.0.1:5000", {
      auth: {
        token: session.access_token
      },
      transports: ["websocket", "polling"],
      reconnection: true,
      reconnectionAttempts: 5,
      reconnectionDelay: 1000,
    });

    newSocket.on("connect", () => {
      console.log(`[Socket] Connected: ${newSocket.id}`);
      newSocket.emit("join_buyer_room", user.id);
      toast.success("Real-time updates enabled");
    });

    newSocket.on("connect_error", async (err: any) => {
      if (err.message?.includes("token")) {
        const newToken = await refreshAccessToken();
        if (newToken) {
          newSocket.io.opts.query = { token: newToken };
          newSocket.connect();
        }
      }
    });

    setSocket(newSocket);
    return () => { newSocket.disconnect(); setSocket(null); };
  }, [session?.access_token, user?.id]);

  /** --- SIGN UP --- */
  const signUp = async (params: SignUpParams) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/auth/signup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(params),
        credentials: "include",
      });
      const data = await safeParse(res);
      if (!res.ok) throw new Error(data?.error || "Signup failed");

      if (data?.email_confirmation_sent) {
        toast.info("Check your email to confirm your account");
        return { error: null };
      }

      handleOAuthLogin(data);
      toast.success("Account created successfully!");
      return { error: null };
    } catch (err: any) {
      toast.error(err.message || "Signup failed");
      return { error: err };
    }
  };

  /** --- SIGN IN --- */
  const signIn = async (email: string, password: string) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
        credentials: "include",
      });
      const data = await safeParse(res);
      if (!res.ok) throw new Error(data?.error || "Invalid credentials");

      handleOAuthLogin(data);

      if (data?.twofa_required) toast.info("Two-factor authentication required");
      toast.success("Logged in successfully");
      return { error: null };
    } catch (err: any) {
      toast.error(err.message || "Login failed");
      return { error: err };
    }
  };

  /** --- ADMIN LOGIN --- */
  const adminLogin = async (email: string, password: string) => {
    try {
      setLoading(true);
      const res = await fetch(`${API_BASE_URL}/api/auth/admin/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
        credentials: "include",
      });
      const data = await safeParse(res);
      if (!res.ok) throw new Error(data?.error || "Admin login failed");

      handleOAuthLogin(data);

      toast.success(`Welcome back, Admin (${data.user.admin_level || "Standard"})`);
      return { error: null };
    } catch (err: any) {
      toast.error(err.message || "Admin login failed");
      return { error: err };
    } finally { setLoading(false); }
  };

  /** --- LOGOUT --- */
  const signOut = async () => {
    try {
      await fetch(`${API_BASE_URL}/api/auth/logout`, {
        method: "POST",
        headers: { Authorization: `Bearer ${localStorage.getItem("access_token")}` },
        credentials: "include",
      }).catch(() => {});

      socket?.disconnect();
      localStorage.clear();
      setSession(null);
      setUser(null);
      setUserRole(null);
      setIsAdmin(false);
      setAdminLevel(null);
      setSocket(null);
      toast.success("Logged out successfully");
    } catch (err) {
      console.error(err);
      localStorage.clear();
      setSession(null);
      setUser(null);
      setUserRole(null);
      setIsAdmin(false);
      setAdminLevel(null);
      setSocket(null);
      toast.error("Logout failed – session cleared");
    }
  };

  /** --- 2FA --- */
  const send2FA = async () => {
    await fetch(`${API_BASE_URL}/api/auth/twofa/setup`, { method: "POST", credentials: "include" });
    toast.info("2FA setup initiated – check your authenticator app");
  };
  const verify2FA = async (code: string) => {
    const res = await fetch(`${API_BASE_URL}/api/auth/twofa/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
      credentials: "include",
    });
    return res.ok;
  };

  return (
    <AuthContext.Provider
      value={{
        session,
        user,
        loading,
        signUp,
        signIn,
        adminLogin,
        signOut,
        userRole,
        isAdmin,
        adminLevel,
        socket,
        handleOAuthLogin,
        refreshAccessToken,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used within AuthProvider");
  return context;
};