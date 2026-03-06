// src/context/AuthContext.tsx
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

type SignUpParams = {
  email: string;
  password: string;
  full_name?: string;
  phone?: string | null;
  role?: "buyer" | "seller";
};

type AuthContextType = {
  session: any | null;
  user: any | null;
  loading: boolean;
  signUp: (params: SignUpParams) => Promise<{ error: Error | null }>;
  signIn: (email: string, password: string) => Promise<{ error: Error | null }>;
  signOut: () => Promise<void>;
  adminLogin: (email: string, password: string) => Promise<{ error: Error | null }>;
  userRole: "buyer" | "seller" | "admin" | null;
  isAdmin: boolean;
  adminLevel?: string | null;
  socket: Socket | null;
};

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<any | null>(null);
  const [user, setUser] = useState<any | null>(null);
  const [loading, setLoading] = useState(true);
  const [userRole, setUserRole] = useState<"buyer" | "seller" | "admin" | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [adminLevel, setAdminLevel] = useState<string | null>(null);
  const [socket, setSocket] = useState<Socket | null>(null);

  const safeParse = async (res: Response) => {
    try {
      return await res.json();
    } catch {
      return null;
    }
  };

  // Restore session from localStorage on mount
  useEffect(() => {
    const loadAuth = async () => {
      setLoading(true);
      try {
        const accessToken = localStorage.getItem("access_token");
        if (!accessToken) {
          setLoading(false);
          return;
        }

        const res = await fetch(`${API_BASE_URL}/api/auth/me`, {
          headers: {
            Authorization: `Bearer ${accessToken}`,
          },
          credentials: "include",
        });

        if (!res.ok) {
          throw new Error(`Session check failed: ${res.status}`);
        }

        const data = await safeParse(res);
        if (data?.user) {
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
      } finally {
        setLoading(false);
      }
    };

    loadAuth();
  }, []);

  // Socket.IO – Connect ONLY after successful login
  useEffect(() => {
    // No valid token/session → clean up
    if (!session?.access_token || !user?.id) {
      if (socket) {
        console.log("[Socket] No valid session/token → disconnecting");
        socket.removeAllListeners();
        socket.disconnect();
        setSocket(null);
      }
      return;
    }

    // Already connected → skip
    if (socket?.connected) {
      console.log("[Socket] Already connected – skipping new connection");
      return;
    }

    console.log("[Socket] Establishing authenticated Socket.IO connection");

    const newSocket = io(SOCKET_URL, {
      query: {
        token: session.access_token, // Backend expects ?token=...
      },
      withCredentials: true,
      transports: ["websocket", "polling"],
      reconnection: true,
      reconnectionAttempts: 5,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 5000,
      timeout: 20000,
    });

    newSocket.on("connect", () => {
      console.log(`[Socket] CONNECTED successfully! ID: ${newSocket.id}`);
      newSocket.emit("join_buyer_room", user.id); // Optional: join private room
      toast.success("Real-time updates enabled");
    });

    newSocket.on("connect_error", (err: Error) => {
      console.error("[Socket] Connection error:", err.message);
      if (err.message.includes("invalid") || err.message.includes("token")) {
        toast.error("Authentication failed – please log in again");
        signOut(); // Auto-logout on auth failure
      }
    });

    newSocket.on("disconnect", (reason: string) => {
      console.warn("[Socket] Disconnected. Reason:", reason);
      if (reason === "io server disconnect") {
        console.log("[Socket] Server forced disconnect – attempting reconnect");
        newSocket.connect();
      }
    });

    newSocket.on("error", (err: any) => {
      console.error("[Socket] Server error event:", err.message || err);
      toast.error("Real-time connection issue: " + (err.message || "Unknown"));
    });

    // Optional: Server confirmation
    newSocket.on("connected", (data: any) => {
      console.log("[Socket] Server confirmed connection:", data);
    });

    setSocket(newSocket);

    // Cleanup on unmount or session change
    return () => {
      console.log("[Socket] Cleaning up socket connection");
      newSocket.removeAllListeners();
      newSocket.disconnect();
      setSocket(null);
    };
  }, [session?.access_token, user?.id]);

  // SIGN UP
  const signUp = async (params: SignUpParams) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/auth/signup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(params),
        credentials: "include",
      });

      const data = await safeParse(res);

      if (!res.ok) {
        throw new Error(data?.error || "Signup failed");
      }

      if (data?.email_confirmation_sent) {
        toast.info("Check your email to confirm your account");
        return { error: null };
      }

      localStorage.setItem("access_token", data.access_token);
      localStorage.setItem("refresh_token", data.refresh_token);

      setSession({ access_token: data.access_token });
      setUser(data.user);
      setUserRole(data.user.role);
      setIsAdmin(false);
      setAdminLevel(null);

      toast.success("Account created successfully!");
      return { error: null };
    } catch (err: any) {
      toast.error(err.message || "Signup failed");
      return { error: err };
    }
  };

  // USER LOGIN
  const signIn = async (email: string, password: string) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
        credentials: "include",
      });

      const data = await safeParse(res);

      if (!res.ok) {
        throw new Error(data?.error || "Invalid credentials");
      }

      localStorage.setItem("access_token", data.access_token);
      localStorage.setItem("refresh_token", data.refresh_token);

      setSession({ access_token: data.access_token });
      setUser(data.user);
      setUserRole(data.user.role);
      setIsAdmin(data.user.role === "admin");
      setAdminLevel(data.user.admin_level || null);

      toast.success("Logged in successfully");
      return { error: null };
    } catch (err: any) {
      toast.error(err.message || "Login failed");
      return { error: err };
    }
  };

  // ADMIN LOGIN – Improved error handling
  const adminLogin = async (email: string, password: string) => {
    try {
      setLoading(true);

      const res = await fetch(`${API_BASE_URL}/api/auth/admin-login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
        credentials: "include",
      });

      let data;
      try {
        data = await safeParse(res);
      } catch {
        data = null;
      }

      if (!res.ok) {
        if (res.status === 401) {
          throw new Error("Invalid email or password");
        }
        if (res.status === 403) {
          throw new Error("Not an admin account – please contact support");
        }
        if (res.status === 500 || res.status === 204) {
          throw new Error("Server error during admin check – try again later");
        }
        throw new Error(data?.error || `Admin login failed (${res.status})`);
      }

      localStorage.setItem("access_token", data.access_token);
      localStorage.setItem("refresh_token", data.refresh_token || "");

      setSession({ access_token: data.access_token });
      setUser(data.user);
      setUserRole("admin");
      setIsAdmin(true);
      setAdminLevel(data.user.admin_level || "standard");

      toast.success(`Welcome back, Admin (${data.user.admin_level || "Standard"})`);
      return { error: null };
    } catch (err: any) {
      let message = err.message || "Admin login failed. Please try again.";

      if (message.includes("Not an admin account")) {
        message = "This account is not registered as an admin.";
      } else if (message.includes("Invalid email or password")) {
        message = "Incorrect email or password.";
      } else if (message.includes("Server error")) {
        message = "Server issue – please try again later.";
      }

      toast.error(message);
      console.error("[adminLogin] Error:", err);
      return { error: new Error(message) };
    } finally {
      setLoading(false);
    }
  };

  // LOGOUT
  const signOut = async () => {
    try {
      await fetch(`${API_BASE_URL}/api/auth/logout`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${localStorage.getItem("access_token")}`,
        },
        credentials: "include",
      }).catch(() => {});

      if (socket) {
        console.log("[Socket] Logging out → disconnecting socket");
        socket.removeAllListeners();
        socket.disconnect();
      }

      localStorage.clear();

      setSession(null);
      setUser(null);
      setUserRole(null);
      setIsAdmin(false);
      setAdminLevel(null);
      setSocket(null);

      toast.success("Logged out successfully");
    } catch (err) {
      console.error("Logout error:", err);
      toast.error("Logout failed – clearing session anyway");

      if (socket) {
        socket.removeAllListeners();
        socket.disconnect();
      }

      localStorage.clear();
      setSession(null);
      setUser(null);
      setUserRole(null);
      setIsAdmin(false);
      setAdminLevel(null);
      setSocket(null);
    }
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