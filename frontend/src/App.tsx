/**
 * App shell and routing.
 *
 * Routes are filtered by role, which is a convenience so the navigation is not
 * cluttered with screens a user cannot use. It is NOT the access control —
 * that lives on the server, where every request is checked regardless of what
 * the client renders. Anyone can open dev tools and call the API directly;
 * only the backend's 403 stops them.
 */

import { useEffect, useState } from "react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { health } from "./api/client";
import { useAuth } from "./auth/AuthContext";
import { Spinner } from "./components/common";
import { AdminPage } from "./pages/AdminPage";
import { DonorDashboard } from "./pages/DonorDashboard";
import { ImpactPage } from "./pages/ImpactPage";
import { Login } from "./pages/Login";
import { RecipientDashboard } from "./pages/RecipientDashboard";
import { WorkflowView } from "./pages/WorkflowView";
import type { UserRole } from "./api/types";

interface NavItem {
  path: string;
  label: string;
  roles: UserRole[];
}

const NAV: NavItem[] = [
  { path: "/donor", label: "Donate", roles: ["DONOR", "ADMIN"] },
  { path: "/recipient", label: "Available food", roles: ["RECIPIENT", "ADMIN"] },
  { path: "/workflow", label: "Workflow", roles: ["DONOR", "ADMIN"] },
  { path: "/impact", label: "Impact", roles: ["DONOR", "RECIPIENT", "VOLUNTEER", "ADMIN"] },
  { path: "/admin", label: "Safety queue", roles: ["ADMIN"] },
];

export function App() {
  const { isAuthenticated, loading, role, fullName, logout } = useAuth();

  if (loading) {
    return (
      <div className="boot">
        <Spinner label="Starting FoodLoop" />
      </div>
    );
  }

  if (!isAuthenticated) return <Login />;

  const visible = NAV.filter((item) => role && item.roles.includes(role));
  const home = visible[0]?.path ?? "/impact";

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          FoodLoop
        </div>

        <nav className="nav">
          {visible.map((item) => (
            <NavLink key={item.path} to={item.path} label={item.label} />
          ))}
        </nav>

        <div className="topbar-right">
          <HealthDot />
          <span className="whoami">
            {fullName}
            <span className="role">{role?.toLowerCase()}</span>
          </span>
          <button type="button" className="btn btn-sm" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>

      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to={home} replace />} />
          <Route path="/donor" element={<Guard roles={["DONOR", "ADMIN"]}><DonorDashboard /></Guard>} />
          <Route path="/recipient" element={<Guard roles={["RECIPIENT", "ADMIN"]}><RecipientDashboard /></Guard>} />
          <Route path="/workflow" element={<Guard roles={["DONOR", "ADMIN"]}><WorkflowView /></Guard>} />
          <Route path="/impact" element={<ImpactPage />} />
          <Route path="/admin" element={<Guard roles={["ADMIN"]}><AdminPage /></Guard>} />
          <Route path="*" element={<Navigate to={home} replace />} />
        </Routes>
      </main>

      <footer className="footer">
        <p>
          Simulated providers are active by default — LLM extraction, route
          estimates and notifications. Every affected number carries a
          provenance tag. The safety ruleset is an illustrative demo, not
          verified against any authority.
        </p>
      </footer>
    </div>
  );
}

function NavLink({ to, label }: { to: string; label: string }) {
  const location = useLocation();
  const active = location.pathname.startsWith(to);
  return (
    <Link to={to} className={`nav-link ${active ? "nav-link-on" : ""}`}>
      {label}
    </Link>
  );
}

function Guard({ roles, children }: { roles: UserRole[]; children: React.ReactNode }) {
  const { role } = useAuth();
  if (!role || !roles.includes(role)) {
    return (
      <div className="page">
        <div className="box box-error">
          <strong>Not available for your role</strong>
          <p>
            Your account is a {role?.toLowerCase()}. The server would refuse
            these requests anyway — this screen is hidden as a convenience, not
            as the security boundary.
          </p>
        </div>
      </div>
    );
  }
  return <>{children}</>;
}

/** Live backend health, so a broken API is obvious rather than mysterious. */
function HealthDot() {
  const [state, setState] = useState<"unknown" | "ok" | "degraded" | "down">("unknown");
  const [detail, setDetail] = useState("");

  useEffect(() => {
    let cancelled = false;
    const check = () => {
      health()
        .then((h) => {
          if (cancelled) return;
          setState(h.status === "ok" ? "ok" : "degraded");
          setDetail(
            `db ${h.db} · postgis ${h.postgis ?? "missing"} · migration ${h.migration ?? "none"}`,
          );
        })
        .catch(() => {
          if (cancelled) return;
          setState("down");
          setDetail("the API is not reachable");
        });
    };
    check();
    const timer = setInterval(check, 30_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <span className={`health health-${state}`} title={detail}>
      <span className="health-dot" aria-hidden="true" />
      {state === "ok" ? "api" : state}
    </span>
  );
}
