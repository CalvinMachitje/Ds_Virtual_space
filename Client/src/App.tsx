// src/App.tsx
import { Toaster } from "@/components/ui/toaster";
import { Toaster as Sonner, toast } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  Outlet,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { useEffect } from 'react';

// Pages
import Index from "./pages/shared/Index";
import LoginPage from "./pages/Auth/LoginPage";
import SignupPage from "./pages/Auth/SignupPage";
import ForgotPassword from "./pages/shared/ForgotPassword";
import ResetPassword from "./pages/shared/ResetPassword";
import NotFound from "./pages/shared/NotFound";
import Gigs from "./pages/shared/Gigs";
import GigDetail from "./pages/shared/GigDetail";
import BuyerProfile from "./pages/Buyer/BuyerProfile";
import Settings from "./pages/shared/Settings";
import MyTickets from "@/pages/support/MyTickets";
import TicketDetail from "@/pages/support/TicketDetail";

// Dashboard & Marketplace Pages
import BuyerDashboard from "./pages/Buyer/BuyerDashboard";
import SellerDashboard from "./pages/Seller/SellerDashboard";
import SellerProfile from "./pages/Seller/SellerProfile";
import CreateGig from "./pages/Seller/CreateGig";
import BookingPage from "./pages/shared/BookingPage";
import CategoryPage from "./pages/shared/CategoryPage";
import BuyerMessagesPage from "./pages/Buyer/BuyerMessagePage";
import SellerMessagesPage from "./pages/Seller/SellerMessagesPage";
import VerificationStatus from "./pages/shared/VerificationStatus";
import ReviewBooking from "./pages/shared/ReviewBooking";
import MyGigs from "./pages/Seller/MyGigs";
import EditGig from "./pages/Seller/EditGig";
import MyBookings from "./pages/Buyer/MyBookings";
import SellerBookings from "./pages/Seller/SellerBookings";
import Chat from "./pages/shared/Chat";

// Admin Pages
import AdminDashboard from "./pages/admin/AdminDashboard";
import UsersAdmin from "./pages/admin/UsersAdmin";
import GigsAdmin from "./pages/admin/GigsAdmin";
import BookingsAdmin from "./pages/admin/BookingAdmin";
import VerificationsAdmin from "./pages/admin/VerificationsAdmin";
import PaymentsAdmin from "./pages/admin/PaymentsAdmin";
import AnalyticsAdmin from "./pages/admin/AnalyticsAdmin";
import SettingsAdmin from "./pages/admin/SettingsAdmin";
import SupportAdmin from "./pages/admin/SupportAdmin";
import LogsAdmin from "./pages/admin/LogsAdmin";
import AdminProfile from "./pages/admin/AdminProfile";
import EmailAdmin from "./pages/admin/EmailAdmin";

// Isolated Admin Login
import AdminLogin from "./pages/admin/AdminLogin";

// Auth & Layout
import { useAuth } from "@/context/AuthContext";
import BottomNav from "@/components/layout/NavLayout";
import OAuthCallback from "./pages/Auth/OAuthCallBack";

const queryClient = new QueryClient();

// ────────────────────────────────────────────────
// Layouts with sidebar offset
// ────────────────────────────────────────────────
const SharedProtectedLayout = () => (
  <div className="min-h-screen bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900 flex flex-col md:ml-64">
    <main className="flex-1 pb-20 md:pb-0 overflow-y-auto">
      <Outlet />
    </main>
    <BottomNav children={""} />
  </div>
);

const AdminLayout = () => (
  <div className="min-h-screen bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900 flex flex-col md:ml-64">
    <main className="flex-1 pb-20 md:pb-0 overflow-y-auto">
      <Outlet />
    </main>
    <BottomNav children={""} />
  </div>
);

// ────────────────────────────────────────────────
// Role guard component
// ────────────────────────────────────────────────
type AllowedRoles = 'buyer' | 'seller' | 'admin';

const RequireRole = ({ children, allowedRoles }: { children: React.ReactNode; allowedRoles: AllowedRoles[] }) => {
  const { session, loading, userRole, isAdmin } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    if (loading) return;

    if (!session) {
      toast.error("Please log in to access this page");
      navigate("/login", { replace: true, state: { from: location } });
      return;
    }

    let currentRole: AllowedRoles | null = null;

    if (isAdmin) currentRole = "admin";
    else if (userRole === "buyer") currentRole = "buyer";
    else if (userRole === "seller") currentRole = "seller";

    if (!currentRole || !allowedRoles.includes(currentRole)) {
      toast.error(`Access denied. ${currentRole ? `(${currentRole})` : ""} users cannot access this area.`);

      const redirectMap: Record<AllowedRoles, string> = {
        buyer: "/dashboard",
        seller: "/dashboard",
        admin: "/admin",
      };

      const redirectTo = currentRole ? redirectMap[currentRole] : "/login";
      navigate(redirectTo, { replace: true });
    }
  }, [session, loading, userRole, isAdmin, navigate, allowedRoles, location]);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen text-white bg-slate-950 md:ml-64">
        Verifying access...
      </div>
    );
  }

  return <>{children}</>;
};

// ────────────────────────────────────────────────
// Dashboard switcher based on role
// ────────────────────────────────────────────────
const DashboardSwitcher = () => {
  const { userRole, isAdmin } = useAuth();
  if (isAdmin) return <Navigate to="/admin" replace />;
  return userRole === "seller" ? <SellerDashboard /> : <BuyerDashboard />;
};

const App = () => {
  const { session, loading } = useAuth();

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen text-white bg-slate-950">
        Loading application...
      </div>
    );
  }

  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <Toaster />
        <Sonner />
        <BrowserRouter>
          <Routes>
            {/* Public routes */}
            <Route path="/" element={session ? <Navigate to="/dashboard" replace /> : <Index />} />
            <Route path="/login" element={<LoginPage />} />
            <Route path="/admin-login" element={<AdminLogin />} />
            <Route path="/signup" element={<SignupPage />} />
            <Route path="/forgot-password" element={<ForgotPassword />} />
            <Route path="/reset-password" element={<ResetPassword />} />
            <Route path="/auth/callback" element={<OAuthCallback />} />

            {/* Public marketplace routes */}
            <Route path="/gigs" element={<Gigs />} />
            <Route path="/gig/:id" element={<GigDetail />} />
            <Route path="/category/:slug" element={<CategoryPage />} />

            {/* Protected routes with role checks */}
            <Route element={<RequireRole allowedRoles={["buyer", "seller", "admin"]}><SharedProtectedLayout /></RequireRole>}>
              <Route path="/dashboard" element={<DashboardSwitcher />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/booking/:id" element={<BookingPage />} />
              <Route path="/chat/:id" element={<Chat />} />
              <Route path="/verification/:id" element={<VerificationStatus />} />
              <Route path="/review-booking/:id" element={<ReviewBooking />} />
              <Route path="/support" element={<MyTickets />} />
              <Route path="/support/:ticketId" element={<TicketDetail />} />

              {/* Buyer-only */}
              <Route element={<RequireRole allowedRoles={["buyer"]}><Outlet /></RequireRole>}>
                <Route path="/my-bookings" element={<MyBookings />} />
                <Route path="/messages" element={<BuyerMessagesPage />} />
                <Route path="/profile/:id" element={<BuyerProfile />} />
              </Route>

              {/* Seller-only */}
              <Route element={<RequireRole allowedRoles={["seller"]}><Outlet /></RequireRole>}>
                <Route path="/create-gig" element={<CreateGig />} />
                <Route path="/my-gigs" element={<MyGigs />} />
                <Route path="/edit-gig/:id" element={<EditGig />} />
                <Route path="/seller-bookings" element={<SellerBookings />} />
                <Route path="/messages" element={<SellerMessagesPage />} />
                <Route path="/seller-profile/:id" element={<SellerProfile />} />
              </Route>

              {/* Admin-only – using separate layout if needed */}
              <Route element={<RequireRole allowedRoles={["admin"]}><Outlet /></RequireRole>}>
                <Route path="/admin" element={<AdminDashboard />} />
                <Route path="/admin/users" element={<UsersAdmin />} />
                <Route path="/admin/gigs" element={<GigsAdmin />} />
                <Route path="/admin/bookings" element={<BookingsAdmin />} />
                <Route path="/admin/verifications" element={<VerificationsAdmin />} />
                <Route path="/admin/payments" element={<PaymentsAdmin />} />
                <Route path="/admin/analytics" element={<AnalyticsAdmin />} />
                <Route path="/admin/settings" element={<SettingsAdmin />} />
                <Route path="/admin/support" element={<SupportAdmin />} />
                <Route path="/admin/logs" element={<LogsAdmin />} />
                <Route path="/admin/profile" element={<AdminProfile />} />
                <Route path="/admin/email" element={<EmailAdmin />} />
              </Route>
            </Route>

            {/* 404 */}
            <Route path="*" element={<NotFound />} />
          </Routes>
        </BrowserRouter>
      </TooltipProvider>
    </QueryClientProvider>
  );
};

export default App;