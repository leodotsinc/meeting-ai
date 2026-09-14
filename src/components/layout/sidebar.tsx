"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { motion, AnimatePresence } from "motion/react";
import {
  ChevronDown,
  Folder,
  Home,
  Mic,
  Plus,
  Settings,
  Tags,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useProjects } from "@/lib/hooks/use-projects";
import { DynamicIcon } from "@/components/ui/icon-picker";
import { ReleaseVersion } from "./release-version";

interface SidebarProps {
  isOpen?: boolean;
  onClose?: () => void;
}

const navigation = [
  { name: "Dashboard", href: "/", icon: Home },
  { name: "All Meetings", href: "/meetings", icon: Mic },
  { name: "New Meeting", href: "/meetings/new", icon: Plus },
];

// Custom event name for sidebar refresh
export const SIDEBAR_REFRESH_EVENT = "sidebar-refresh";

export function Sidebar({ isOpen = true, onClose }: SidebarProps) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { projects, isLoading: isLoadingProjects, refresh } = useProjects();
  const [projectsExpanded, setProjectsExpanded] = useState(true);

  const activeProjectId = searchParams.get("project");

  // Listen for sidebar refresh events (e.g., when a meeting is deleted)
  // This is a fallback for components that still use the event system
  useEffect(() => {
    const handleRefresh = () => {
      refresh();
    };

    window.addEventListener(SIDEBAR_REFRESH_EVENT, handleRefresh);
    return () => {
      window.removeEventListener(SIDEBAR_REFRESH_EVENT, handleRefresh);
    };
  }, [refresh]);

  const sidebarContent = (
    <div className="flex h-full flex-col">
      <div className="flex-1 space-y-1 overflow-y-auto px-3 py-4">
        {navigation.map((item, index) => {
          const isActive = pathname === item.href ||
            (item.href !== "/" && item.href !== "/meetings" && pathname.startsWith(item.href)) ||
            (item.href === "/meetings" && pathname === "/meetings" && !activeProjectId);

          return (
            <motion.div
              key={item.name}
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: index * 0.05 }}
            >
              <Link href={item.href} onClick={onClose}>
                <span
                  className={cn(
                    "group flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-all duration-200",
                    isActive
                      ? "bg-zinc-900 text-zinc-50 dark:bg-zinc-100 dark:text-zinc-900"
                      : "text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-50"
                  )}
                >
                  <item.icon
                    className={cn(
                      "h-5 w-5 transition-transform duration-200 group-hover:scale-110",
                      isActive
                        ? "text-zinc-50 dark:text-zinc-900"
                        : "text-zinc-400 group-hover:text-zinc-600 dark:text-zinc-500 dark:group-hover:text-zinc-300"
                    )}
                  />
                  {item.name}
                </span>
              </Link>
            </motion.div>
          );
        })}

        {/* Projects Section */}
        <div className="pt-4">
          <button
            onClick={() => setProjectsExpanded(!projectsExpanded)}
            className="flex w-full items-center justify-between px-3 py-2 text-xs font-semibold uppercase tracking-wider text-zinc-400 transition-colors hover:text-zinc-600 dark:text-zinc-500 dark:hover:text-zinc-300"
          >
            <span>Projects</span>
            <motion.div
              animate={{ rotate: projectsExpanded ? 0 : -90 }}
              transition={{ duration: 0.2 }}
            >
              <ChevronDown className="h-4 w-4" />
            </motion.div>
          </button>

          <AnimatePresence>
            {projectsExpanded && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                <div className="space-y-0.5 pt-1">
                  {isLoadingProjects ? (
                    <div className="flex items-center justify-center py-4">
                      <div className="h-4 w-4 animate-spin rounded-full border-2 border-zinc-300 border-t-zinc-600 dark:border-zinc-700 dark:border-t-zinc-400" />
                    </div>
                  ) : projects.length === 0 ? (
                    <p className="px-3 py-2 text-xs text-zinc-400 dark:text-zinc-500">
                      No projects yet
                    </p>
                  ) : (
                    projects.map((project, index) => {
                      const isActive = pathname === "/meetings" && activeProjectId === project.id;

                      return (
                        <motion.div
                          key={project.id}
                          initial={{ opacity: 0, x: -10 }}
                          animate={{ opacity: 1, x: 0 }}
                          transition={{ delay: index * 0.03 }}
                        >
                          <Link
                            href={`/meetings?project=${project.id}`}
                            onClick={onClose}
                          >
                            <span
                              className={cn(
                                "group flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-all duration-200",
                                isActive
                                  ? "bg-zinc-100 font-medium text-zinc-900 dark:bg-zinc-800 dark:text-zinc-100"
                                  : "text-zinc-600 hover:bg-zinc-50 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800/50 dark:hover:text-zinc-200"
                              )}
                            >
                              {project.icon ? (
                                <DynamicIcon
                                  name={project.icon}
                                  className="h-4 w-4 transition-transform duration-200 group-hover:scale-110"
                                  style={{ color: project.color }}
                                />
                              ) : (
                                <Folder
                                  className="h-4 w-4 transition-transform duration-200 group-hover:scale-110"
                                  style={{ color: project.color }}
                                />
                              )}
                              <span className="flex-1 truncate">{project.name}</span>
                              {project._count?.meetings !== undefined && project._count.meetings > 0 && (
                                <span className="text-xs text-zinc-400 dark:text-zinc-500">
                                  {project._count.meetings}
                                </span>
                              )}
                            </span>
                          </Link>
                        </motion.div>
                      );
                    })
                  )}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        {/* Manage Section */}
        <div className="pt-4">
          <p className="px-3 py-2 text-xs font-semibold uppercase tracking-wider text-zinc-400 dark:text-zinc-500">
            Manage
          </p>
          <Link href="/organize" onClick={onClose}>
            <span
              className={cn(
                "group flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-all duration-200",
                pathname === "/organize"
                  ? "bg-zinc-900 text-zinc-50 dark:bg-zinc-100 dark:text-zinc-900"
                  : "text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-50"
              )}
            >
              <Tags
                className={cn(
                  "h-5 w-5 transition-transform duration-200 group-hover:scale-110",
                  pathname === "/organize"
                    ? "text-zinc-50 dark:text-zinc-900"
                    : "text-zinc-400 group-hover:text-zinc-600 dark:text-zinc-500 dark:group-hover:text-zinc-300"
                )}
              />
              Projects & Tags
            </span>
          </Link>
          <Link href="/settings" onClick={onClose}>
            <span
              className={cn(
                "group flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-all duration-200",
                pathname === "/settings"
                  ? "bg-zinc-900 text-zinc-50 dark:bg-zinc-100 dark:text-zinc-900"
                  : "text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-50"
              )}
            >
              <Settings
                className={cn(
                  "h-5 w-5 transition-transform duration-200 group-hover:scale-110",
                  pathname === "/settings"
                    ? "text-zinc-50 dark:text-zinc-900"
                    : "text-zinc-400 group-hover:text-zinc-600 dark:text-zinc-500 dark:group-hover:text-zinc-300"
                )}
              />
              Settings
            </span>
          </Link>
        </div>
      </div>

      <div className="border-t border-zinc-200 p-4 dark:border-zinc-800">
        <ReleaseVersion />
      </div>
    </div>
  );

  return (
    <>
      {/* Desktop Sidebar */}
      <aside className="hidden h-screen w-64 flex-shrink-0 border-r border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950 lg:block">
        {sidebarContent}
      </aside>

      {/* Mobile Sidebar Overlay */}
      <AnimatePresence>
        {isOpen && (
          <>
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="fixed inset-0 z-40 bg-black/50 backdrop-blur-sm lg:hidden"
              onClick={onClose}
            />
            <motion.aside
              initial={{ x: -280 }}
              animate={{ x: 0 }}
              exit={{ x: -280 }}
              transition={{ type: "spring", damping: 25, stiffness: 200 }}
              className="fixed inset-y-0 left-0 z-50 w-72 border-r border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950 lg:hidden"
            >
              <div className="flex h-16 items-center justify-between border-b border-zinc-200 px-4 dark:border-zinc-800">
                <span className="text-lg font-semibold">Menu</span>
                <Button variant="ghost" size="icon" onClick={onClose}>
                  <X className="h-5 w-5" />
                </Button>
              </div>
              {sidebarContent}
            </motion.aside>
          </>
        )}
      </AnimatePresence>
    </>
  );
}
