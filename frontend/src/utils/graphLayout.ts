import type { GraphData, GraphNode } from '@/types'

export interface LayoutNode extends GraphNode {
  x: number
  y: number
  vx: number
  vy: number
  fx?: number | null
  fy?: number | null
  /** 节点实际占用的宽高，布局和连线都使用它们计算边界。 */
  w: number
  h: number
}

export interface LayoutResult {
  nodes: LayoutNode[]
  width: number
  height: number
}

export interface LayoutOptions {
  width?: number
  height?: number
  iterations?: number
  nodePadding?: number
  nodeSize?: (node: GraphNode) => { width: number; height: number }
}

/**
 * 面向业务本体的稳定布局：网格初始化 + 轻量边吸引 + 矩形碰撞消解。
 *
 * 旧实现只按圆形半径计算斥力，而本体节点实际是宽矩形，节点数量增加后
 * 很容易出现“中心点不重叠、卡片却重叠”的情况。这里把真实节点宽高纳入
 * 计算，并在最后做确定性的碰撞消解，保证布局结果可读且不会互相覆盖。
 */
export function forceLayout(data: GraphData, opts: LayoutOptions = {}): LayoutResult {
  const width = Math.max(720, opts.width || 1200)
  const height = Math.max(420, opts.height || 700)
  const iterations = opts.iterations ?? 180
  const padding = opts.nodePadding ?? 34
  const sizeOf = opts.nodeSize || ((node: GraphNode) => {
    const size = (node.size || 20) * 2
    return { width: size, height: size }
  })

  const sizes = data.nodes.map((node) => {
    const size = sizeOf(node)
    return {
      width: Math.max(32, size.width),
      height: Math.max(32, size.height),
    }
  })
  const maxWidth = Math.max(120, ...sizes.map((size) => size.width))
  const maxHeight = Math.max(56, ...sizes.map((size) => size.height))
  const count = Math.max(1, data.nodes.length)
  const columns = Math.max(1, Math.ceil(Math.sqrt((count * width) / height)))
  const rows = Math.max(1, Math.ceil(count / columns))
  const cellWidth = Math.max(maxWidth + padding, width / columns)
  const cellHeight = Math.max(maxHeight + padding, height / rows)
  const layoutWidth = Math.max(width, columns * cellWidth)
  const layoutHeight = Math.max(height, rows * cellHeight)

  const nodes: LayoutNode[] = data.nodes.map((node, index) => {
    const size = sizes[index]
    const col = index % columns
    const row = Math.floor(index / columns)
    return {
      ...node,
      x: (col + 0.5) * cellWidth,
      y: (row + 0.5) * cellHeight,
      vx: 0,
      vy: 0,
      fx: null,
      fy: null,
      w: size.width,
      h: size.height,
    }
  })

  // A large ontology is better served by the deterministic grid than by the
  // O(n²) force/collision passes. Keeping the grid also preserves every node
  // and edge for interaction while preventing menu navigation from blocking
  // the main thread on hundreds of nodes.
  if (count > 180) {
    return { nodes, width: layoutWidth, height: layoutHeight }
  }

  const byId = new Map(nodes.map((node) => [node.id, node]))
  const edges = data.edges.filter((edge) => byId.has(edge.source) && byId.has(edge.target))
  const idealEdgeLength = Math.max(170, Math.min(360, Math.sqrt((layoutWidth * layoutHeight) / count) * 0.88))

  for (let iteration = 0; iteration < iterations; iteration++) {
    for (const node of nodes) {
      node.vx *= 0.78
      node.vy *= 0.78
    }

    // 矩形碰撞斥力：同时考虑卡片宽度和高度。
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i]
        const b = nodes[j]
        let dx = a.x - b.x
        let dy = a.y - b.y
        const distance = Math.hypot(dx, dy) || 0.01
        const minDistance = Math.hypot((a.w + b.w) / 2, (a.h + b.h) / 2) + padding
        if (distance < minDistance) {
          if (Math.abs(dx) < 0.01 && Math.abs(dy) < 0.01) {
            dx = (i % 2 ? 1 : -1) * 0.01
            dy = (j % 2 ? 1 : -1) * 0.01
          }
          const push = Math.min(16, (minDistance - distance) * 0.14)
          a.vx += (dx / distance) * push
          a.vy += (dy / distance) * push
          b.vx -= (dx / distance) * push
          b.vy -= (dy / distance) * push
        }
      }
    }

    // 有关系的节点保持适度靠近，避免退化成无意义的规则网格。
    for (const edge of edges) {
      const a = byId.get(edge.source)!
      const b = byId.get(edge.target)!
      const dx = b.x - a.x
      const dy = b.y - a.y
      const distance = Math.hypot(dx, dy) || 0.01
      const force = Math.max(-4, Math.min(4, (distance - idealEdgeLength) * 0.012))
      a.vx += (dx / distance) * force
      a.vy += (dy / distance) * force
      b.vx -= (dx / distance) * force
      b.vy -= (dy / distance) * force
    }

    // 轻微中心约束，避免组件漂移到虚拟画布边缘。
    for (const node of nodes) {
      node.vx += (layoutWidth / 2 - node.x) * 0.0018
      node.vy += (layoutHeight / 2 - node.y) * 0.0018
      const velocity = Math.hypot(node.vx, node.vy)
      const step = Math.min(12, velocity)
      if (step > 0) {
        node.x += (node.vx / velocity) * step
        node.y += (node.vy / velocity) * step
      }
      keepInside(node, layoutWidth, layoutHeight)
    }
  }

  // 最后的确定性消解是保证“不重叠”的硬约束，不依赖力导向是否收敛。
  for (let pass = 0; pass < 240; pass++) {
    let changed = false
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i]
        const b = nodes[j]
        const overlapX = (a.w + b.w) / 2 + padding - Math.abs(a.x - b.x)
        const overlapY = (a.h + b.h) / 2 + padding - Math.abs(a.y - b.y)
        if (overlapX <= 0 || overlapY <= 0) continue
        changed = true
        const directionX = a.x === b.x ? (i % 2 ? 1 : -1) : Math.sign(a.x - b.x)
        const directionY = a.y === b.y ? (j % 2 ? 1 : -1) : Math.sign(a.y - b.y)
        if (overlapX <= overlapY) {
          const shift = overlapX / 2 + 0.5
          a.x += directionX * shift
          b.x -= directionX * shift
        } else {
          const shift = overlapY / 2 + 0.5
          a.y += directionY * shift
          b.y -= directionY * shift
        }
      }
    }
    if (!changed) break
  }

  // 碰撞消解可能让整体布局向边缘外扩，最后只做一次整体平移，不能逐节点
  // clamp，否则边缘节点会被夹回原位并重新制造重叠。
  const minLeft = Math.min(...nodes.map((node) => node.x - node.w / 2))
  const minTop = Math.min(...nodes.map((node) => node.y - node.h / 2))
  const shiftX = minLeft < 10 ? 10 - minLeft : 0
  const shiftY = minTop < 10 ? 10 - minTop : 0
  for (const node of nodes) {
    node.x += shiftX
    node.y += shiftY
  }
  const maxRight = Math.max(...nodes.map((node) => node.x + node.w / 2))
  const maxBottom = Math.max(...nodes.map((node) => node.y + node.h / 2))
  return {
    nodes,
    width: Math.max(layoutWidth, maxRight + 10),
    height: Math.max(layoutHeight, maxBottom + 10),
  }
}

function keepInside(node: LayoutNode, width: number, height: number) {
  node.x = Math.max(node.w / 2 + 10, Math.min(width - node.w / 2 - 10, node.x))
  node.y = Math.max(node.h / 2 + 10, Math.min(height - node.h / 2 - 10, node.y))
}

/** 计算两点间贝塞尔控制点（用于弯曲边，避免重叠）。 */
export function edgePath(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  curve = 0.18,
): { path: string; mx: number; my: number } {
  const dx = x2 - x1
  const dy = y2 - y1
  const cx = (x1 + x2) / 2 - dy * curve
  const cy = (y1 + y2) / 2 + dx * curve
  const path = `M ${x1} ${y1} Q ${cx} ${cy} ${x2} ${y2}`
  const mx = 0.25 * x1 + 0.5 * cx + 0.25 * x2
  const my = 0.25 * y1 + 0.5 * cy + 0.25 * y2
  return { path, mx, my }
}
