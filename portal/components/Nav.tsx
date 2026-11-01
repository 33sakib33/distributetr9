"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";

export type NavLink = { href: string; label: string };

/**
 * The header navigation. Admin pages sit behind one "Admin" menu so the bar
 * stays on a line. Under about 720px everything collapses behind a menu button.
 */
export default function Nav({
  links,
  adminLinks = [],
  email,
}: {
  links: NavLink[];
  adminLinks?: NavLink[];
  email: string | null;
}) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [adminOpen, setAdminOpen] = useState(false);
  const adminBox = useRef<HTMLDivElement>(null);
  const adminButton = useRef<HTMLButtonElement>(null);

  // A navigation closes the menus.
  useEffect(() => {
    setOpen(false);
    setAdminOpen(false);
  }, [pathname]);

  // A click or focus outside the admin menu closes it.
  useEffect(() => {
    if (!adminOpen) return;
    function outside(e: Event) {
      if (adminBox.current && !adminBox.current.contains(e.target as Node)) setAdminOpen(false);
    }
    document.addEventListener("mousedown", outside);
    document.addEventListener("focusin", outside);
    return () => {
      document.removeEventListener("mousedown", outside);
      document.removeEventListener("focusin", outside);
    };
  }, [adminOpen]);

  function current(href: string) {
    if (href === "/") return pathname === "/";
    // The admin dashboard lives at /admin; its sub pages have their own links.
    if (href === "/admin") return pathname === "/admin";
    return pathname === href || pathname.startsWith(`${href}/`);
  }

  const inAdmin = pathname === "/admin" || pathname.startsWith("/admin/");

  return (
    <>
      <button
        type="button"
        className="ghost nav-toggle"
        aria-expanded={open}
        aria-controls="site-nav"
        onClick={() => setOpen((v) => !v)}
      >
        {open ? "Close menu" : "Menu"}
      </button>
      <nav id="site-nav" aria-label="Main" className={open ? "open" : undefined}>
        {links.map((l) => (
          <Link key={l.href} href={l.href} aria-current={current(l.href) ? "page" : undefined}>
            {l.label}
          </Link>
        ))}
        {adminLinks.length ? (
          <div
            className="nav-menu"
            ref={adminBox}
            onKeyDown={(e) => {
              if (e.key === "Escape" && adminOpen) {
                setAdminOpen(false);
                adminButton.current?.focus();
              }
            }}
          >
            <button
              ref={adminButton}
              type="button"
              className={`nav-menu-button${inAdmin ? " current" : ""}`}
              aria-expanded={adminOpen}
              aria-controls="admin-menu"
              onClick={() => setAdminOpen((v) => !v)}
            >
              Admin <span aria-hidden="true">▾</span>
            </button>
            {adminOpen ? (
              <ul id="admin-menu" className="nav-menu-list" aria-label="Admin pages">
                {adminLinks.map((l) => (
                  <li key={l.href}>
                    <Link href={l.href} aria-current={current(l.href) ? "page" : undefined}>
                      {l.label}
                    </Link>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
        {email ? (
          <>
            <span className="muted nav-email" title={email}>
              {email}
            </span>
            <Link href="/auth/logout">Sign out</Link>
          </>
        ) : (
          <Link href="/login">Sign in</Link>
        )}
      </nav>
    </>
  );
}
