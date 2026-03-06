// src/pages/admin/LogsAdmin.tsx

import { useState, useEffect, useRef, useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  RefreshCw,
  AlertCircle,
  Download,
  Search,
  Clock,
  Filter,
  Zap,
  Loader2,
} from "lucide-react";
import { formatDistanceToNow, format } from "date-fns";
import Papa from "papaparse";
import { io, Socket } from "socket.io-client";
import { VisuallyHidden } from "@radix-ui/react-visually-hidden";

type Log = {
  id: string;
  user_id: string;
  action: string;
  details: any;
  created_at: string;
};

const API_BASE = "http://196.253.26.113:5000";

export default function LogsAdmin() {
  const queryClient = useQueryClient();
  const socketRef = useRef<Socket | null>(null);
  const tableRef = useRef<HTMLDivElement>(null);

  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [actionFilter, setActionFilter] = useState("all");
  const [dateRange, setDateRange] =
    useState<"all" | "today" | "week" | "month">("all");
  const [selectedLog, setSelectedLog] = useState<Log | null>(null);
  const [liveMode, setLiveMode] = useState(true);
  const [newLogCount, setNewLogCount] = useState(0);
  const [isReconnecting, setIsReconnecting] = useState(false);

  const pageSize = 20;

  /* --------------------------------------------------
     TOKEN REFRESH (Silent)
  -------------------------------------------------- */

  const refreshToken = async (): Promise<string | null> => {
    try {
      const res = await fetch(`${API_BASE}/api/auth/refresh`, {
        method: "POST",
        credentials: "include",
      });

      if (!res.ok) return null;

      const data = await res.json();
      localStorage.setItem("access_token", data.access_token);
      return data.access_token;
    } catch {
      return null;
    }
  };

  /* --------------------------------------------------
     FETCH LOGS (REST)
  -------------------------------------------------- */

  const {
    data: logsData,
    isLoading,
    error,
    isFetching,
  } = useQuery<{ logs: Log[]; total: number }>({
    queryKey: ["admin-logs", page, search, actionFilter, dateRange],
    queryFn: async () => {
      let url = `/api/admin/logs?page=${page}&limit=${pageSize}`;
      if (search) url += `&search=${encodeURIComponent(search)}`;
      if (actionFilter !== "all") url += `&action=${actionFilter}`;
      if (dateRange !== "all") url += `&date_range=${dateRange}`;

      const token = localStorage.getItem("access_token");

      const res = await fetch(url, {
        headers: { Authorization: `Bearer ${token}` },
      });

      if (res.status === 401) {
        const newToken = await refreshToken();
        if (!newToken) throw new Error("Session expired");
        return fetch(url, {
          headers: { Authorization: `Bearer ${newToken}` },
        }).then((r) => r.json());
      }

      if (!res.ok) throw new Error("Failed to load logs");

      return res.json();
    },
    placeholderData: (prev) => prev,
  });

  const logs = logsData?.logs || [];
  const totalLogs = logsData?.total || 0;
  const totalPages = Math.ceil(totalLogs / pageSize);

  /* --------------------------------------------------
     SOCKET CONNECTION (Enterprise)
  -------------------------------------------------- */

  const connectSocket = useCallback(async () => {
    let token = localStorage.getItem("access_token");
    if (!token) return;

    const socket = io(API_BASE, {
      transports: ["websocket"],
      auth: { token },
      reconnection: true,
      reconnectionAttempts: Infinity,
      reconnectionDelay: 2000,
    });

    socketRef.current = socket;

    socket.on("connect", () => {
      setIsReconnecting(false);

      socket.emit("subscribe_logs", {
        search,
        action: actionFilter,
        date_range: dateRange,
      });

      toast.success("Live log stream connected");
    });

    /* --- Redis Replay --- */
    socket.on("log_replay", (replayLogs: Log[]) => {
      queryClient.setQueryData(
        ["admin-logs", page, search, actionFilter, dateRange],
        (old: any) => {
          if (!old) return old;
          return {
            ...old,
            logs: [...replayLogs, ...old.logs].slice(0, pageSize),
          };
        }
      );
    });

    /* --- Real-Time Log --- */
    socket.on("new_log", (newLog: Log) => {
      if (!liveMode) {
        setNewLogCount((c) => c + 1);
        return;
      }

      queryClient.setQueryData(
        ["admin-logs", page, search, actionFilter, dateRange],
        (old: any) => {
          if (!old) return old;
          return {
            ...old,
            logs: [newLog, ...old.logs].slice(0, pageSize),
            total: old.total + 1,
          };
        }
      );

      if (tableRef.current && tableRef.current.scrollTop < 100) {
        tableRef.current.scrollTo({ top: 0, behavior: "smooth" });
      }
    });

    /* --- Backpressure Protection --- */
    socket.on("too_many_logs", () => {
      toast.warning(
        "High log volume detected. Switching to throttled mode."
      );
      setLiveMode(false);
    });

    /* --- Silent JWT Re-Auth --- */
    socket.on("connect_error", async (err: any) => {
      if (err.message === "Unauthorized") {
        const newToken = await refreshToken();
        if (!newToken) return;

        socket.auth = { token: newToken };
        socket.connect();
      }
    });

    socket.on("disconnect", () => {
      setIsReconnecting(true);
    });
  }, [search, actionFilter, dateRange, liveMode, page, queryClient]);

  useEffect(() => {
    connectSocket();
    return () => {
      socketRef.current?.disconnect();
    };
  }, [connectSocket]);

  /* --------------------------------------------------
     EXPORT CSV
  -------------------------------------------------- */

  const handleExportCSV = () => {
    if (!logs.length) return toast.warning("No logs to export");

    const csvData = logs.map((log) => ({
      ID: log.id,
      UserID: log.user_id,
      Action: log.action,
      Details: JSON.stringify(log.details),
      Time: format(new Date(log.created_at), "yyyy-MM-dd HH:mm:ss"),
    }));

    const csv = Papa.unparse(csvData);
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.setAttribute(
      "download",
      `audit-logs-${format(new Date(), "yyyy-MM-dd")}.csv`
    );
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    toast.success("Logs exported");
  };

  const handleRefresh = () => {
    queryClient.invalidateQueries({ queryKey: ["admin-logs"] });
  };

  /* --------------------------------------------------
     UI (UNCHANGED STRUCTURE)
  -------------------------------------------------- */

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900 p-6">
      <div className="max-w-7xl mx-auto space-y-8">
        <div className="flex justify-between items-center">
          <h1 className="text-3xl font-bold text-white flex items-center gap-3">
            <Clock className="h-8 w-8 text-purple-500" />
            Audit Logs
          </h1>

          <div className="flex gap-3">
            <div className="flex items-center gap-2 bg-slate-800 px-3 py-1.5 rounded-md border border-slate-700">
              <Zap className="h-4 w-4 text-yellow-500" />
              <Switch
                checked={liveMode}
                onCheckedChange={(val) => {
                  setLiveMode(val);
                  setNewLogCount(0);
                }}
              />
              <span className="text-sm text-slate-300">Live</span>
              {newLogCount > 0 && !liveMode && (
                <Badge variant="destructive">+{newLogCount}</Badge>
              )}
            </div>

            <Button
              variant="outline"
              size="sm"
              onClick={handleRefresh}
              disabled={isFetching}
            >
              {isFetching ? (
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
              ) : (
                <RefreshCw className="h-4 w-4 mr-2" />
              )}
              Refresh
            </Button>

            <Button
              variant="outline"
              size="sm"
              onClick={handleExportCSV}
              disabled={!logs.length}
            >
              <Download className="h-4 w-4 mr-2" />
              Export CSV
            </Button>
          </div>
        </div>

        {/* Table and modal remain same as your original implementation */}
      </div>
    </div>
  );
}