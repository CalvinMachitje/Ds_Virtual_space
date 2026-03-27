/* eslint-disable @typescript-eslint/no-explicit-any */
// src/pages/admin/AdminDashboard.tsx

import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import {ShieldCheck,Users,Briefcase,MessageSquare,BarChart,Search as SearchIcon,RefreshCw,AlertCircle,CreditCard,} from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import { toast } from "sonner";
import "react-loading-skeleton/dist/skeleton.css";

interface DashboardStats {
  total_users: number;
  pending_verifications: number;
  open_tickets: number;
  active_gigs: number;
  total_revenue?: number;
}

type StatCard = {
  key: keyof DashboardStats;
  title: string;
  icon: any;
  color: string;
  description: string;
};

export default function AdminDashboard() {
  const { isAdmin, userRole, loading } = useAuth();

  const [searchTerm, setSearchTerm] = useState("");

  // ─────────────────────────────────────────────
  // Fetch Dashboard Stats (React Query)
  // ─────────────────────────────────────────────
  const {
    data: stats,
    isLoading,
    error,
    refetch,
  } = useQuery<DashboardStats>({
    queryKey: ["admin-dashboard"],
    queryFn: async () => {
      const token = localStorage.getItem("access_token");
      if (!token) throw new Error("Authentication required");

      const res = await fetch("http://127.0.0.1:5000/api/admin/dashboard", {
        headers: { 
          Authorization: `Bearer ${token}` 
        },
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.error || "Failed to load dashboard");
      }

      return res.json();
    },
    enabled: !!isAdmin && userRole === "admin" && !loading,
    staleTime: 60 * 1000,
  });

  useEffect(() => {
    if (!loading && (!isAdmin || userRole !== "admin")) {
      toast.error("Access denied. Admin only.");
    }
  }, [isAdmin, userRole, loading]);

  if (!isAdmin || userRole !== "admin") {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900 p-6">
        <div className="text-center">
          <AlertCircle className="h-16 w-16 mx-auto mb-4 text-red-500" />
          <h2 className="text-2xl font-bold mb-2 text-white">
            Access Denied
          </h2>
          <p className="text-slate-400">
            This page is restricted to administrators only.
          </p>
        </div>
      </div>
    );
  }

  // ─────────────────────────────────────────────
  // Stat Cards Config
  // ─────────────────────────────────────────────
  const statCards: StatCard[] = [
    {
      key: "total_users",
      title: "Total Users",
      icon: Users,
      color: "text-blue-400",
      description: "Registered platform accounts",
    },
    {
      key: "pending_verifications",
      title: "Pending Verifications",
      icon: ShieldCheck,
      color: "text-yellow-400",
      description: "Awaiting approval",
    },
    {
      key: "open_tickets",
      title: "Open Support Tickets",
      icon: MessageSquare,
      color: "text-red-400",
      description: "Unresolved user issues",
    },
    {
      key: "active_gigs",
      title: "Active Gigs",
      icon: Briefcase,
      color: "text-green-400",
      description: "Published listings",
    },
    {
      key: "total_revenue",
      title: "Total Revenue",
      icon: CreditCard,
      color: "text-purple-400",
      description: "Platform earnings",
    },
  ];

  // ─────────────────────────────────────────────
  // Filter Stats Like Gigs Page
  // ─────────────────────────────────────────────
  const filteredCards = useMemo(() => {
    if (!searchTerm.trim()) return statCards;

    return statCards.filter((card) =>
      card.title.toLowerCase().includes(searchTerm.toLowerCase())
    );
  }, [searchTerm]);

  // ─────────────────────────────────────────────
  // Render
  // ─────────────────────────────────────────────
  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900 p-6">
      <div className="max-w-7xl mx-auto">
        {/* Header */}
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-8">
          <h1 className="text-4xl font-bold text-white flex items-center gap-3">
            <BarChart className="h-8 w-8 text-indigo-400" />
            Admin Dashboard
          </h1>

          <Button
            onClick={() => refetch()}
            className="bg-indigo-600 hover:bg-indigo-700 flex items-center gap-2"
          >
            <RefreshCw className="h-4 w-4" />
            Refresh
          </Button>
        </div>

        {/* Search */}
        <div className="relative mb-8">
          <SearchIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-5 w-5 text-slate-400" />
          <Input
            placeholder="Search dashboard stats..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="pl-10 bg-slate-900/60 border-slate-700 text-white placeholder:text-slate-500"
          />
        </div>

        {/* Error State */}
        {error && (
          <div className="text-center text-red-400 mb-8">
            <p>Failed to load dashboard</p>
            <p className="text-sm text-slate-400">
              {(error as Error).message}
            </p>
          </div>
        )}

        {/* Stats Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
          {isLoading
            ? [...Array(6)].map((_, i) => (
                <Skeleton key={i} className="h-40 rounded-xl" />
              ))
            : filteredCards.map((card) => {
                const Icon = card.icon;
                const value = stats?.[card.key];

                return (
                  <Card
                    key={card.key}
                    className="bg-slate-900/70 border-slate-700 hover:border-indigo-500/50 transition-all duration-300"
                  >
                    <CardHeader className="pb-2">
                      <CardTitle className="text-white flex items-center gap-2 text-lg">
                        <Icon className={`h-5 w-5 ${card.color}`} />
                        {card.title}
                      </CardTitle>
                    </CardHeader>

                    <CardContent>
                      <p className="text-3xl font-bold text-white">
                        {value != null
                          ? typeof value === "number"
                            ? value.toLocaleString()
                            : value
                          : "—"}
                      </p>

                      <p className="text-sm text-slate-400 mt-1">
                        {card.description}
                      </p>

                      <Badge
                        variant="outline"
                        className="mt-4 border-slate-600 text-slate-400"
                      >
                        Live Data
                      </Badge>
                    </CardContent>
                  </Card>
                );
              })}
        </div>
      </div>
    </div>
  );
}