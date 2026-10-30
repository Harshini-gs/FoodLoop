/**
 * Auth context.
 *
 * Holds the token and the current role so the router can show the right
 * screens. Worth being explicit about what this is NOT: it is not security.
 * Hiding a route from a role is a convenience so the UI is not cluttered —
 * the actual check happens on the server for every request, which is why
 * `require_roles` exists in the backend's deps.py. Anyone can open dev tools
 * and call the API directly; only the server's 403 stops them.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { ApiRequestError, auth, getToken, setToken } from "../api/client";
import type { UserRole } from "../api/types";

interface AuthState {
  token: string | null;
  role: UserRole | null;
  userId: string | null;
  orgId: string | null;
  email: string | null;
  fullName: string | null;
  loading: boolean;
  error: string | null;
}

interface AuthContextValue extends AuthState {
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  isAuthenticated: boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

const EMPTY: AuthState = {
  token: null,
  role: null,
  userId: null,
  orgId: null,
  email: null,
  fullName: null,
  loading: true,
  error: null,
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(EMPTY);

  // On mount, if a token is stored, confirm it is still valid. A token in
  // localStorage proves nothing — it may be expired or the account disabled.
  useEffect(() => {
    const stored = getToken();
    if (!stored) {
      setState({ ...EMPTY, loading: false });
      return;
    }

    let cancelled = false;
    auth
      .me()
      .then((user) => {
        if (cancelled) return;
        setState({
          token: stored,
          role: user.role as UserRole,
          userId: user.id,
          orgId: user.org_id,
          email: user.email,
          fullName: user.full_name,
          loading: false,
          error: null,
        });
      })
      .catch(() => {
        if (cancelled) return;
        setToken(null);
        setState({ ...EMPTY, loading: false });
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const token = await auth.login(email, password);
      setToken(token.access_token);
      const user = await auth.me();
      setState({
        token: token.access_token,
        role: token.role,
        userId: token.user_id,
        orgId: token.org_id,
        email: user.email,
        fullName: user.full_name,
        loading: false,
        error: null,
      });
    } catch (err) {
      setToken(null);
      const message =
        err instanceof ApiRequestError ? err.message : "could not reach the server";
      setState({ ...EMPTY, loading: false, error: message });
      throw err;
    }
  }, []);

  const logout = useCallback(() => {
    setToken(null);
    setState({ ...EMPTY, loading: false });
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ ...state, login, logout, isAuthenticated: state.token !== null }),
    [state, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (ctx === null) throw new Error("useAuth must be used inside an AuthProvider");
  return ctx;
}
