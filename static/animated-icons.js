const AnimatedIcons = {
    Sun: `
<svg viewBox="0 0 48 48" width="48" height="48">
  <style>
    @keyframes spin { 100% { transform: rotate(360deg); } }
    @keyframes pulse { 0%, 100% { opacity: 0.4; } 50% { opacity: 1; } }
    @keyframes scaleSun { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.15); } }
  </style>
  <g style="animation: spin 12s linear infinite; transform-origin: 24px 24px;">
    ${[0, 45, 90, 135, 180, 225, 270, 315].map(deg => \`
      <line x1="24" y1="6" x2="24" y2="10" stroke="#FBBF24" stroke-width="2" stroke-linecap="round"
        style="transform-origin: 24px 24px; transform: rotate(\${deg}deg); animation: pulse 2s infinite; animation-delay: \${deg/360}s;" />
    \`).join('')}
  </g>
  <circle cx="24" cy="24" r="8" fill="#FBBF24" opacity="0.2" style="animation: scaleSun 3s infinite ease-in-out; transform-origin: 24px 24px;" />
  <circle cx="24" cy="24" r="8" stroke="#FBBF24" stroke-width="2" fill="none" />
</svg>`,

    Cloud: `
<svg viewBox="0 0 48 48" width="48" height="48">
  <style>
    @keyframes sway { 0%, 100% { transform: translateX(0); } 25% { transform: translateX(2px); } 75% { transform: translateX(-2px); } }
  </style>
  <g style="animation: sway 6s infinite ease-in-out;">
    <path d="M36 30H14a8 8 0 01-.5-16A10 10 0 0134 16a7 7 0 012 14z" fill="#94A3B8" opacity="0.12" />
    <path d="M36 30H14a8 8 0 01-.5-16A10 10 0 0134 16a7 7 0 012 14z" stroke="#94A3B8" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none" />
  </g>
</svg>`,

    PartlyCloudy: `
<svg viewBox="0 0 48 48" width="48" height="48">
  <style>
    @keyframes spinCloudy { 100% { transform: rotate(360deg); } }
    @keyframes swayCloudy { 0%, 100% { transform: translateX(0); } 25% { transform: translateX(1.5px); } 75% { transform: translateX(-1.5px); } }
  </style>
  <g style="animation: spinCloudy 20s linear infinite; transform-origin: 16px 16px;">
    ${[0, 60, 120, 180, 240, 300].map(deg => \`
      <line x1="16" y1="6" x2="16" y2="9" stroke="#FBBF24" stroke-width="1.5" stroke-linecap="round" style="transform-origin: 16px 16px; transform: rotate(\${deg}deg);" />
    \`).join('')}
  </g>
  <circle cx="16" cy="16" r="6" stroke="#FBBF24" stroke-width="1.5" fill="#FBBF24" fill-opacity="0.15" />
  <g style="animation: swayCloudy 5s infinite ease-in-out;">
    <path d="M38 34H18a7 7 0 01-.5-14A9 9 0 0136 22a6 6 0 012 12z" fill="#94A3B8" opacity="0.12" />
    <path d="M38 34H18a7 7 0 01-.5-14A9 9 0 0136 22a6 6 0 012 12z" stroke="#94A3B8" stroke-width="2" stroke-linecap="round" fill="none" />
  </g>
</svg>`,

    Rain: `
<svg viewBox="0 0 48 48" width="48" height="48">
  <style>
    @keyframes rainDrop { 0% { transform: translateY(0); opacity: 0.8; } 100% { transform: translateY(12px); opacity: 0; } }
  </style>
  <path d="M36 22H14a7 7 0 01-.5-14A9 9 0 0134 10a6 6 0 012 12z" fill="#60A5FA" opacity="0.1" />
  <path d="M36 22H14a7 7 0 01-.5-14A9 9 0 0134 10a6 6 0 012 12z" stroke="#60A5FA" stroke-width="2" stroke-linecap="round" fill="none" />
  ${[{x:16,d:0}, {x:22,d:0.3}, {x:28,d:0.6}, {x:34,d:0.15}].map(drop => \`
    <line x1="\${drop.x}" y1="26" x2="\${drop.x}" y2="30" stroke="#60A5FA" stroke-width="2" stroke-linecap="round"
      style="animation: rainDrop 0.8s infinite ease-in; animation-delay: \${drop.d}s;" />
  \`).join('')}
</svg>`,

    HeavyRain: `
<svg viewBox="0 0 48 48" width="48" height="48">
  <style>
    @keyframes heavyRainDrop { 0% { transform: translateY(0); opacity: 1; } 100% { transform: translateY(16px); opacity: 0; } }
  </style>
  <path d="M36 20H14a7 7 0 01-.5-14A9 9 0 0134 8a6 6 0 012 12z" fill="#3B82F6" opacity="0.1" />
  <path d="M36 20H14a7 7 0 01-.5-14A9 9 0 0134 8a6 6 0 012 12z" stroke="#3B82F6" stroke-width="2" stroke-linecap="round" fill="none" />
  ${[{x:14,d:0}, {x:19,d:0.15}, {x:24,d:0.3}, {x:29,d:0.1}, {x:34,d:0.4}, {x:37,d:0.25}].map(drop => \`
    <line x1="\${drop.x}" y1="24" x2="\${drop.x - 2}" y2="30" stroke="#3B82F6" stroke-width="1.5" stroke-linecap="round"
      style="animation: heavyRainDrop 0.6s infinite ease-in; animation-delay: \${drop.d}s;" />
  \`).join('')}
</svg>`,

    Snow: `
<svg viewBox="0 0 48 48" width="48" height="48">
  <style>
    @keyframes snowFlake1 { 0% { transform: translate(0, 0); opacity: 0.8; } 50% { transform: translate(3px, 7px); } 100% { transform: translate(0, 14px); opacity: 0; } }
    @keyframes snowFlake2 { 0% { transform: translate(0, 0); opacity: 0.8; } 50% { transform: translate(-3px, 7px); } 100% { transform: translate(0, 14px); opacity: 0; } }
  </style>
  <path d="M36 22H14a7 7 0 01-.5-14A9 9 0 0134 10a6 6 0 012 12z" fill="#CBD5E1" opacity="0.15" />
  <path d="M36 22H14a7 7 0 01-.5-14A9 9 0 0134 10a6 6 0 012 12z" stroke="#CBD5E1" stroke-width="2" stroke-linecap="round" fill="none" />
  ${[{x:16,d:0}, {x:22,d:0.5}, {x:28,d:0.2}, {x:34,d:0.7}, {x:19,d:1.0}, {x:31,d:0.4}].map((f, i) => \`
    <circle cx="\${f.x}" cy="26" r="1.5" fill="#CBD5E1"
      style="animation: \${i % 2 === 0 ? 'snowFlake1' : 'snowFlake2'} 2s infinite ease-in; animation-delay: \${f.d}s;" />
  \`).join('')}
</svg>`,

    Thunder: `
<svg viewBox="0 0 48 48" width="48" height="48">
  <style>
    @keyframes flashBolt { 0%, 30%, 100% { opacity: 0; } 10%, 20% { opacity: 1; } }
    @keyframes flashFill { 0%, 30%, 100% { opacity: 0; } 10%, 20% { opacity: 0.3; } }
  </style>
  <path d="M36 20H14a7 7 0 01-.5-14A9 9 0 0134 8a6 6 0 012 12z" fill="#F59E0B" opacity="0.08" />
  <path d="M36 20H14a7 7 0 01-.5-14A9 9 0 0134 8a6 6 0 012 12z" stroke="#94A3B8" stroke-width="2" stroke-linecap="round" fill="none" />
  <path d="M26 20l-3 8h6l-3 10" stroke="#F59E0B" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" fill="none" style="animation: flashBolt 3s infinite;" />
  <path d="M26 20l-3 8h6l-3 10" fill="#F59E0B" style="animation: flashFill 3s infinite;" />
</svg>`,

    Fog: `
<svg viewBox="0 0 48 48" width="48" height="48">
  <style>
    @keyframes fogSway { 0%, 100% { transform: translateX(0); opacity: 0.2; } 50% { transform: translateX(3px); opacity: 0.6; } }
  </style>
  ${[{y:16,w:28,d:0}, {y:22,w:32,d:0.5}, {y:28,w:24,d:1.0}, {y:34,w:30,d:1.5}].map(l => \`
    <line x1="\${24 - l.w/2}" y1="\${l.y}" x2="\${24 + l.w/2}" y2="\${l.y}" stroke="#94A3B8" stroke-width="2.5" stroke-linecap="round"
      style="animation: fogSway 3s infinite ease-in-out; animation-delay: \${l.d}s;" />
  \`).join('')}
</svg>`,

    Tornado: `
<svg viewBox="0 0 48 48" width="48" height="48">
  <style>
    @keyframes tornadoSway { 0%, 100% { transform: translateX(-2px); } 50% { transform: translateX(2px); } }
  </style>
  <g style="animation: tornadoSway 0.5s infinite ease-in-out;">
    <path d="M12 16h24M16 24h16M20 32h8M22 40h4" stroke="#94A3B8" stroke-width="3" stroke-linecap="round" fill="none" />
  </g>
</svg>`
};
