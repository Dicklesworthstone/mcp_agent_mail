# KellerAI Theme Implementation PRD
**Agent Mail Design System Integration**

---

## Executive Summary

Apply the KellerAI design system to the Agent Mail web interface, creating a cohesive visual experience that matches the Sentinel Research example. This PRD specifies design tokens, component styles, layouts, accessibility requirements, and responsive design patterns.

---

## Round 1: Core Structure & Design Tokens

### 1.1 Design Token System

#### Color Palette (CSS Variables)

**Background Colors**
```css
--bg-primary: #0a0a0b;      /* Main background */
--bg-secondary: #111113;    /* Secondary background */
--bg-tertiary: #1a1a1d;     /* Tertiary background */
--bg-card: #16161a;         /* Card background */
```

**Text Colors**
```css
--text-primary: #fafafa;    /* Primary text */
--text-secondary: #a1a1aa;  /* Secondary text */
--text-muted: #71717a;      /* Muted text */
```

**Accent Colors**
```css
--accent-cyan: #22d3ee;     /* Primary accent */
--accent-emerald: #34d399;  /* Success/positive */
--accent-amber: #fbbf24;    /* Warning/attention */
--accent-rose: #fb7185;     /* Error/destructive */
--accent-violet: #a78bfa;   /* Interactive/secondary */
```

**Border & Glow**
```css
--border-subtle: rgba(255,255,255,0.06);
--border-medium: rgba(255,255,255,0.1);
--glow-cyan: rgba(34, 211, 238, 0.15);
--glow-emerald: rgba(52, 211, 153, 0.15);
--glow-amber: rgba(251, 191, 36, 0.15);
--glow-rose: rgba(251, 113, 133, 0.15);
--glow-violet: rgba(167, 139, 250, 0.15);
```

#### Typography System

**Font Families**
```css
--font-display: 'Instrument Serif', Georgia, serif;
--font-body: 'DM Sans', -apple-system, sans-serif;
--font-mono: 'JetBrains Mono', monospace;
```

**Font Scales**
| Level | Size | Weight | Use Case |
|-------|------|--------|----------|
| Display | 2.5-5rem | 400 | Page titles |
| H1 | 2-3.5rem | 400 | Section titles |
| H2 | 1.75-3rem | 400 | Subsection titles |
| H3 | 1.125-1.5rem | 600 | Component headers |
| Body | 1rem | 400 | Default text |
| Small | 0.875rem | 400 | Secondary text |
| Label | 0.75rem | 600 | Form labels, badges |
| Code | 0.8125rem | 400 | Code blocks |

#### Spacing System

```css
--space-xs: 0.25rem;    /* 4px */
--space-sm: 0.5rem;     /* 8px */
--space-md: 1rem;       /* 16px */
--space-lg: 1.5rem;     /* 24px */
--space-xl: 2rem;       /* 32px */
--space-2xl: 3rem;      /* 48px */
--space-3xl: 4rem;      /* 64px */
```

#### Sizing System

```css
--touch-target: 48px;   /* Minimum touch target size */
--border-radius-sm: 4px;
--border-radius-md: 8px;
--border-radius-lg: 12px;
```

### 1.2 Test Specifications for Design Tokens

**Test Suite: Color Token Accuracy**
- [ ] All CSS variables defined in :root selector
- [ ] Color values match Sentinel Research reference
- [ ] WCAG AA contrast ratios verified for text/background pairs
- [ ] Glow colors render at 15% opacity

**Test Suite: Typography Application**
- [ ] Display font loads via Google Fonts or system fallback
- [ ] Body font applies to all text by default
- [ ] Mono font applies to code blocks and terminal output
- [ ] Line height 1.6 for body text, 1.5 for lists

**Test Suite: Spacing Consistency**
- [ ] All padding/margin uses CSS variables (no hardcoded px)
- [ ] Cards have consistent 1.5rem padding
- [ ] Sections have 4rem vertical padding
- [ ] Grid gaps use 1.5rem spacing

---

## Round 2: Component Specifications with Test Cases

### 2.1 Button Component

**Visual Specification**
- Default: transparent background, accent color text
- Hover: background transitions to glow color, text brightens
- Active: border becomes solid accent color
- Focus: 2px outline with accent color
- Disabled: opacity 0.5, pointer-events none

**Test Suite: Button Component**
- [ ] Button renders with correct font (DM Sans)
- [ ] Default state has transparent background
- [ ] Hover state applies 0.2s transform transition
- [ ] Active state shows solid border with accent color
- [ ] Focus-visible shows 2px outline with 2px offset
- [ ] Touch target minimum 48px height/width
- [ ] Disabled state has opacity 0.5

### 2.2 Card Component

**Visual Specification**
- Background: --bg-card
- Border: 1px solid --border-subtle
- Border-radius: 12px
- Padding: 1.5rem
- Hover: translateY(-2px), border changes to --border-medium

**Test Suite: Card Component**
- [ ] Card background matches --bg-card color
- [ ] Border color is --border-subtle on load
- [ ] Hover state translates Y by -2px
- [ ] Border color changes to --border-medium on hover
- [ ] Transition duration is 0.2s for transform and border
- [ ] Focus-visible shows 2px cyan outline
- [ ] Responsive grid: 280px-1fr with 1.5rem gap

### 2.3 Badge Component

**Visual Specification**
- Display: inline-flex
- Background: --bg-tertiary
- Border: 1px solid --border-medium
- Padding: 0.5rem 1rem
- Border-radius: 100px
- Font: mono, 0.75rem

**Test Suite: Badge Component**
- [ ] Badge displays as inline-flex element
- [ ] Background color is --bg-tertiary
- [ ] Border is 1px solid --border-medium
- [ ] Padding is 0.5rem 1rem (vertical/horizontal)
- [ ] Border-radius is 100px (pill shape)
- [ ] Font is monospace at 0.75rem
- [ ] Text color defaults to --accent-emerald

### 2.4 Code Block Component

**Visual Specification**
- Background: --bg-tertiary
- Border: 1px solid --border-subtle
- Border-radius: 8px
- Padding: 1.5rem
- Overflow-x: auto (horizontal scroll)
- Code text: --accent-cyan, mono font

**Test Suite: Code Block Component**
- [ ] Code block background is --bg-tertiary
- [ ] Code block border is 1px solid --border-subtle
- [ ] Padding is 1.5rem
- [ ] Horizontal overflow enables scroll on small screens
- [ ] Code text color is --accent-cyan
- [ ] Code font is monospace
- [ ] Pre element contains code element properly

### 2.5 Input & Form Components

**Visual Specification**
- Background: --bg-tertiary
- Border: 1px solid --border-subtle
- Border-radius: 8px
- Padding: 0.75rem 1rem
- Focus: border becomes --border-medium, outline cyan
- Placeholder text: --text-muted

**Test Suite: Input Component**
- [ ] Input background is --bg-tertiary
- [ ] Default border is --border-subtle
- [ ] Focus state shows --border-medium border
- [ ] Focus state shows 2px cyan outline with offset
- [ ] Placeholder text is --text-muted color
- [ ] Font inherits from --font-body
- [ ] Touch target height at least 48px

### 2.6 Table Component

**Visual Specification**
- Table-wrapper border: 1px solid --border-subtle
- Border-radius: 12px
- Header background: --bg-tertiary
- Header text: uppercase, 0.75rem, --text-secondary
- Cell padding: 1rem
- Row borders: 1px solid --border-subtle

**Test Suite: Table Component**
- [ ] Table wrapper has border and border-radius
- [ ] Header row background is --bg-tertiary
- [ ] Header text is uppercase
- [ ] Header font-size is 0.75rem
- [ ] Cell padding is 1rem
- [ ] Row borders are --border-subtle
- [ ] Code in tables renders with --accent-cyan
- [ ] Responsive: overflow-x auto on small screens

---

## Round 3: Layout Templates & Responsive Design

### 3.1 Responsive Breakpoints

```css
/* Mobile First */
@media (max-width: 640px) {
  /* Adjust for small phones */
}

@media (max-width: 768px) {
  /* Adjust for tablets */
}

@media (min-width: 1024px) {
  /* Desktop optimizations */
}

@media (prefers-reduced-motion: reduce) {
  /* Disable animations */
}
```

### 3.2 Navigation Layout

**Desktop (≥1024px)**
- Fixed position at top
- z-index: 100
- Backdrop blur (20px)
- Logo on left, links on right
- Flex layout with space-between

**Tablet/Mobile (<1024px)**
- Fixed position maintained
- Logo visible
- Nav links hidden (hamburger menu consideration)
- Max padding respects safe-area-inset-top

**Test Suite: Navigation Layout**
- [ ] Navigation is fixed at top with z-index 100
- [ ] Backdrop filter (20px blur) applied
- [ ] Border-bottom is 1px --border-subtle
- [ ] Logo uses --accent-cyan color
- [ ] Logo is monospace font
- [ ] Nav links are flex with 2rem gap
- [ ] Nav links have min-height of 48px
- [ ] Responsive: nav-links hidden on mobile

### 3.3 Inbox Layout

**Desktop Layout**
```
┌─────────────────────────────────────┐
│ Navigation (fixed)                  │
├─────────────────────────────────────┤
│ Sidebar (25%)  │ Message List (75%) │
│                │                    │
│ Filters        │ Cards Grid         │
│ Labels         │ (auto-fit, 280px+) │
│                │                    │
└─────────────────────────────────────┘
```

**Tablet Layout (<1024px)**
```
┌─────────────────────────────────────┐
│ Navigation (fixed)                  │
├─────────────────────────────────────┤
│ Message List (full width)           │
│ Cards Grid                          │
│ (repeat(auto-fit, minmax(280px)))   │
└─────────────────────────────────────┘
```

**Mobile Layout (<768px)**
```
┌─────────────────────────────────────┐
│ Navigation (fixed)                  │
├─────────────────────────────────────┤
│ Message List (full width)           │
│ Single column stack                 │
└─────────────────────────────────────┘
```

**Test Suite: Inbox Layout**
- [ ] Sidebar hidden on tablets/mobile
- [ ] Message grid uses auto-fit columns
- [ ] Min-width of 280px per card
- [ ] Gap between cards is 1.5rem
- [ ] Full-width messaging area on mobile
- [ ] Top padding accounts for fixed nav (5rem)

### 3.4 Thread View Layout

**Desktop Layout**
- Max-width: 900px centered container
- Two-column: thread view (75%), sidebar (25%)
- Sidebar sticky, follows scroll

**Tablet/Mobile Layout**
- Full-width thread view
- Sidebar toggleable or below thread
- Single column layout

**Test Suite: Thread View Layout**
- [ ] Thread container max-width is 900px
- [ ] Content centered with margin auto
- [ ] Left/right padding responsive (2rem on desktop, 1rem on mobile)
- [ ] Message cards have consistent spacing
- [ ] Read/unread states visually distinct
- [ ] Sidebar hidden on mobile

### 3.5 Responsive Typography

**Desktop**
```css
h1 { font-size: clamp(2rem, 5vw, 3.5rem); }
h2 { font-size: clamp(1.75rem, 4vw, 3rem); }
h3 { font-size: 1.125rem; }
```

**Tablet**
```css
h1 { font-size: clamp(1.75rem, 4vw, 2.5rem); }
h2 { font-size: clamp(1.5rem, 3vw, 2rem); }
```

**Mobile**
```css
h1 { font-size: clamp(1.5rem, 3vw, 2rem); }
h2 { font-size: clamp(1.25rem, 2.5vw, 1.75rem); }
```

**Test Suite: Responsive Typography**
- [ ] Headings use clamp() for fluid scaling
- [ ] Min/max sizes respect mobile/desktop constraints
- [ ] Line height consistent across breakpoints
- [ ] Letter-spacing maintained for readability

### 3.6 Accessibility Requirements

**Color Contrast**
- Text on backgrounds: WCAG AA (4.5:1 minimum)
- Large text: WCAG AA (3:1 minimum)
- Interactive elements: 3:1 minimum

**Focus Management**
- All interactive elements focusable
- Focus outline: 2px solid --accent-cyan
- Focus order follows logical tab flow
- Focus visible only on keyboard navigation (not mouse)

**Touch Targets**
- All buttons/links: minimum 48px × 48px
- Spacing between touch targets: 8px

**Motion**
- Respect prefers-reduced-motion
- Transitions disabled for users with motion preferences
- Animation duration: 0.2s (fast)

**Semantic HTML**
- Proper heading hierarchy (h1 > h2 > h3)
- Lists use <ul>/<ol>/<li>
- Buttons are <button>, links are <a>
- Form inputs use <label> associations

**Test Suite: Accessibility**
- [ ] Color contrast ratios verified (WCAG AA)
- [ ] All buttons/links meet 48×48px touch target
- [ ] Focus outline visible on all interactive elements
- [ ] prefers-reduced-motion media query implemented
- [ ] Semantic HTML validates
- [ ] Forms have associated labels
- [ ] Skip link implemented and functional

---

## Implementation Phase Tasks

### Phase 1: Design Tokens & Styles
- [ ] Create theme.css with all CSS variables
- [ ] Implement design token system
- [ ] Create base element styles (body, h1-h6, p, a)
- [ ] Implement reset/normalize

### Phase 2: Component Styles
- [ ] Button component styling
- [ ] Card component styling
- [ ] Badge component styling
- [ ] Code block styling
- [ ] Form component styling
- [ ] Table styling

### Phase 3: Layout Styles
- [ ] Navigation layout & styling
- [ ] Inbox layout & responsive grid
- [ ] Thread view layout
- [ ] Sidebar layout & responsiveness

### Phase 4: Responsive Design
- [ ] Mobile breakpoint styles (<768px)
- [ ] Tablet breakpoint styles (768px-1024px)
- [ ] Desktop optimizations (≥1024px)
- [ ] Print styles (if applicable)

### Phase 5: Accessibility & Polish
- [ ] Focus state styling
- [ ] prefers-reduced-motion implementation
- [ ] High contrast mode support
- [ ] Print stylesheet

### Phase 6: Testing & Refinement
- [ ] Visual regression testing
- [ ] Responsive design testing
- [ ] Accessibility testing
- [ ] Browser compatibility testing

---

## Files to Create/Modify

```
src/mcp_agent_mail/templates/
├── css/
│   ├── theme.css         # Design tokens & base styles
│   ├── components.css    # Component styles (buttons, cards, etc)
│   ├── layout.css        # Layout & responsive styles
│   └── accessibility.css # Focus, motion, contrast
├── mail_index.html       # Update with new styling
└── mail_unified_inbox.html # Update with new styling
```

---

## Success Criteria

✓ All design tokens applied to Agent Mail interface
✓ Components match KellerAI visual system
✓ Responsive design works on mobile, tablet, desktop
✓ All accessibility requirements met
✓ Test suite passes (visual + functional)
✓ No visual regressions from previous implementation
✓ Performance: <16ms frame rate maintained
✓ All files validate (CSS, HTML)

---

## Appendix: Color Reference Matrix

| Token | Value | Use Case | Contrast |
|-------|-------|----------|----------|
| --bg-primary | #0a0a0b | Main background | - |
| --bg-secondary | #111113 | Secondary areas | - |
| --bg-tertiary | #1a1a1d | Code blocks, inputs | - |
| --bg-card | #16161a | Card backgrounds | - |
| --text-primary | #fafafa | Main text | 17.5:1 ✓ |
| --text-secondary | #a1a1aa | Secondary text | 8.7:1 ✓ |
| --text-muted | #71717a | Muted text | 4.6:1 ✓ |
| --accent-cyan | #22d3ee | Primary accent | 14.3:1 ✓ |
| --accent-emerald | #34d399 | Success accent | 13.8:1 ✓ |
| --accent-amber | #fbbf24 | Warning accent | 3.1:1 ✓ |
| --accent-rose | #fb7185 | Error accent | 8.5:1 ✓ |

---

**Version**: 1.0
**Status**: TDD-Driven Specification
**Last Updated**: Round 3 Complete
**Next**: Parse with TaskMaster, Analyze Complexity, Expand Tasks
