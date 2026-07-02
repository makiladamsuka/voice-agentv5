"use client";

import dynamic from "next/dynamic";

const NavigationMap = dynamic(() => import("@/components/app/isometric-map"), {
  ssr: false,
});

export default function MapClient(props: any) {
  return <NavigationMap {...props} />;
}
