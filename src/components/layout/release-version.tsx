"use client";

import useSWR from "swr";

async function fetchVersion(url: string): Promise<{ version: string }> {
  const response = await fetch(url, { cache: "no-store", credentials: "same-origin" });
  if (!response.ok) throw new Error("Version unavailable");
  return response.json();
}

export function ReleaseVersion() {
  const { data } = useSWR("/api/version", fetchVersion, { shouldRetryOnError: false });

  return (
    <p className="text-xs text-zinc-400 dark:text-zinc-500">
      Meeting AI{data ? ` v${data.version}` : ""}
    </p>
  );
}
