use image::{GrayImage, Luma, Rgb, RgbImage};
use serde_json::{Value, json};

use super::super::{native, raster};
use crate::error::{Error, Result};

type Point = [f64; 2];

#[derive(Clone, Copy)]
pub(super) struct Rect {
    pub(super) x: i32,
    pub(super) y: i32,
    pub(super) w: i32,
    pub(super) h: i32,
}

impl Rect {
    pub(super) fn overlaps(self, other: Self) -> bool {
        self.x < other.x + other.w
            && other.x < self.x + self.w
            && self.y < other.y + other.h
            && other.y < self.y + self.h
    }

    pub(super) fn value(self) -> Value {
        json!({"x":self.x,"y":self.y,"width":self.w,"height":self.h})
    }

    fn ellipse(self) -> (Point, Point) {
        let radius = [
            (self.w as f64 / 2.0).max(0.5),
            (self.h as f64 / 2.0).max(0.5),
        ];
        (
            [
                self.x as f64 + self.w as f64 / 2.0,
                self.y as f64 + self.h as f64 / 2.0,
            ],
            radius,
        )
    }
}

pub(super) struct Tail {
    pub(super) data: Value,
    pub(super) polygon: Vec<Point>,
    controls: Vec<Point>,
}

pub(super) fn tail(rect: Rect, anchor: Point, bounds: [i32; 2], voice: &str) -> Option<Tail> {
    let target = [
        (anchor[0] * f64::from(bounds[0])).round_ties_even(),
        (anchor[1] * f64::from(bounds[1])).round_ties_even(),
    ];
    let (center, radius) = rect.ellipse();
    let delta = [target[0] - center[0], target[1] - center[1]];
    let distance = ((delta[0] / radius[0]).powi(2) + (delta[1] / radius[1]).powi(2)).sqrt();
    if distance <= 1.08 {
        return None;
    }
    let attachment = [
        center[0] + delta[0] / distance,
        center[1] + delta[1] / distance,
    ];
    let vector = [target[0] - attachment[0], target[1] - attachment[1]];
    let source_distance = vector[0].hypot(vector[1]);
    let shortest = f64::from(bounds[0].min(bounds[1]));
    let available = source_distance - (shortest * 0.025).clamp(8.0, 24.0);
    if available < 12.0 {
        return None;
    }
    let maximum = (radius[0].min(radius[1]) * 0.9).min(shortest * 0.12);
    let length = available.min(maximum);
    let unit = [vector[0] / source_distance, vector[1] / source_distance];
    let normal = [-unit[1], unit[0]];
    let half = (length * 0.18).clamp(5.0, 14.0);
    let depth = (0.7 * half).min(10.0);
    let shoulder = (length * 0.30).min(22.0);
    let tip_run = (length * 0.30).min(24.0);
    let tip_half = (half * 0.30).max(2.6);
    let shift = |base: Point, run: f64, width: f64| {
        [
            base[0] + unit[0] * run + normal[0] * width,
            base[1] + unit[1] * run + normal[1] * width,
        ]
    };
    let round = |value: f64| (value * 10000.0).round_ties_even() / 10000.0;
    let point = |p: Point| p.map(round);
    let tip = shift(attachment, length, 0.0);
    let base = [
        point(shift(attachment, -depth, half)),
        point(shift(attachment, -depth, -half)),
    ];
    let shoulder_width = half * 0.92;
    let controls = [
        [
            point(shift(attachment, shoulder, shoulder_width)),
            point(shift(tip, -tip_run, tip_half)),
        ],
        [
            point(shift(attachment, shoulder, -shoulder_width)),
            point(shift(tip, -tip_run, -tip_half)),
        ],
    ];
    let tip = point(tip);
    let a = cubic(base[0], controls[0], tip);
    let b = cubic(base[1], controls[1], tip);
    let polygon = a.into_iter().chain(b.into_iter().rev()).collect();
    let data = json!({
        "attachment":point(attachment),"base":base,"control":controls,"length":round(length),
        "policy_version":"organic-cubic-v1","source_gap":round(source_distance-length),
        "speaker_anchor":anchor,"tip":tip,"voice_source":voice,"width":round(half*2.0),
    });
    Some(Tail {
        data,
        polygon,
        controls: base
            .into_iter()
            .chain(controls.into_iter().flatten())
            .chain([tip])
            .collect(),
    })
}

fn cubic(start: Point, [left, right]: [Point; 2], end: Point) -> [Point; 25] {
    std::array::from_fn(|i| {
        let t = i as f64 / 24.0;
        let u = 1.0 - t;
        std::array::from_fn(|n| {
            u.powi(3) * start[n]
                + 3.0 * u.powi(2) * t * left[n]
                + 3.0 * u * t.powi(2) * right[n]
                + t.powi(3) * end[n]
        })
    })
}

fn edges(poly: &[Point]) -> impl Iterator<Item = (Point, Point)> + '_ {
    poly.iter()
        .copied()
        .zip(poly.iter().copied().cycle().skip(1))
}

pub(super) fn ellipse_collision(polygon: &[Point], rect: Rect) -> bool {
    let (center, radius) = rect.ellipse();
    let polygon: Vec<_> = polygon
        .iter()
        .map(|p| {
            [
                (p[0] - center[0]) / radius[0],
                (p[1] - center[1]) / radius[1],
            ]
        })
        .collect();
    contains(&polygon, [0.0, 0.0])
        || edges(&polygon).any(|(a, b)| {
            let d = [b[0] - a[0], b[1] - a[1]];
            let n = d[0] * d[0] + d[1] * d[1];
            let t = if n == 0.0 {
                0.0
            } else {
                (-(a[0] * d[0] + a[1] * d[1]) / n).clamp(0.0, 1.0)
            };
            (a[0] + t * d[0]).powi(2) + (a[1] + t * d[1]).powi(2) <= 1.0
        })
}

fn orientation(a: Point, b: Point, p: Point) -> i32 {
    let val = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]);
    let scale = a
        .into_iter()
        .chain(b)
        .chain(p)
        .map(f64::abs)
        .fold(1.0, f64::max);
    let limit = 1e-9 * scale * (b[0] - a[0]).hypot(b[1] - a[1]).max(1.0);
    if val > limit {
        1
    } else if val < -limit {
        -1
    } else {
        0
    }
}

fn on_segment(a: Point, b: Point, p: Point) -> bool {
    let scale = a
        .into_iter()
        .chain(b)
        .chain(p)
        .map(f64::abs)
        .fold(1.0, f64::max)
        * 1e-9;
    orientation(a, b, p) == 0
        && (0..2).all(|i| (a[i].min(b[i]) - scale..=a[i].max(b[i]) + scale).contains(&p[i]))
}

fn contains(poly: &[Point], p: Point) -> bool {
    let mut inside = false;
    for (a, b) in edges(poly) {
        if on_segment(a, b, p) {
            return true;
        }
        if (a[1] > p[1]) != (b[1] > p[1])
            && a[0] + (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) > p[0]
        {
            inside = !inside;
        }
    }
    inside
}

pub(super) fn polygon_collision(poly: &[Point], other: &[Point]) -> bool {
    edges(poly).any(|(a, b)| {
        edges(other).any(|(c, d)| {
            (orientation(a, b, c) * orientation(a, b, d) < 0
                && orientation(c, d, a) * orientation(c, d, b) < 0)
                || on_segment(a, b, c)
                || on_segment(a, b, d)
                || on_segment(c, d, a)
                || on_segment(c, d, b)
        })
    }) || contains(poly, other[0])
        || contains(other, poly[0])
}

pub(super) fn balloon(image: &mut RgbImage, rect: Rect, tail: &Tail) -> Result<()> {
    const SCALE: i32 = 6;
    let mut bounds = [
        f64::from(rect.x),
        f64::from(rect.y),
        f64::from(rect.x + rect.w),
        f64::from(rect.y + rect.h),
    ];
    for &[x, y] in &tail.controls {
        bounds = [
            bounds[0].min(x),
            bounds[1].min(y),
            bounds[2].max(x),
            bounds[3].max(y),
        ];
    }
    let (left, top) = (
        0.max(bounds[0].floor() as i32 - 5),
        0.max(bounds[1].floor() as i32 - 5),
    );
    let right = (image.width() as i32).min(bounds[2].ceil() as i32 + 6);
    let bottom = (image.height() as i32).min(bounds[3].ceil() as i32 + 6);
    let (w, h) = ((right - left) as u32, (bottom - top) as u32);
    if u64::from(w) * u64::from(h) * 36 > 1600 * 2400 * 16 {
        return Err(Error::limit("the decoded balloon mask limit"));
    }
    let mut mask = GrayImage::new(w * SCALE as u32, h * SCALE as u32);
    let mut poly: Vec<_> = tail
        .polygon
        .iter()
        .map(|&[x, y]| {
            [
                ((x - left as f64) * f64::from(SCALE)).round_ties_even() as i32,
                ((y - top as f64) * f64::from(SCALE)).round_ties_even() as i32,
            ]
        })
        .collect();
    let ellipse = [
        (rect.x - left) * SCALE,
        (rect.y - top) * SCALE,
        (rect.x + rect.w - left) * SCALE,
        (rect.y + rect.h - top) * SCALE,
    ];
    if !native::draw_balloon(&mut mask, &mut poly, ellipse) {
        return Err(Error::invalid("cannot rasterize a lettering balloon"));
    }
    let outline = dilate(&mask, 12);
    let mask = raster::shrink(&mask, w, h);
    let outline = raster::shrink(&outline, w, h);
    blend_mask(image, &outline, left, top, 15);
    blend_mask(image, &mask, left, top, 255);
    Ok(())
}

fn dilate(mask: &GrayImage, radius: u32) -> GrayImage {
    let horizontal = GrayImage::from_fn(mask.width(), mask.height(), |x, y| {
        Luma([
            (x.saturating_sub(radius)..=(x + radius).min(mask.width() - 1))
                .map(|p| mask.get_pixel(p, y)[0])
                .max()
                .unwrap_or(0),
        ])
    });
    GrayImage::from_fn(mask.width(), mask.height(), |x, y| {
        Luma([
            (y.saturating_sub(radius)..=(y + radius).min(mask.height() - 1))
                .map(|p| horizontal.get_pixel(x, p)[0])
                .max()
                .unwrap_or(0),
        ])
    })
}

pub(super) fn blend_mask(image: &mut RgbImage, mask: &GrayImage, left: i32, top: i32, ink: u8) {
    for (x, y, _) in mask.enumerate_pixels().filter(|(_, _, p)| p[0] != 0) {
        let (x, y) = (x as i32 + left, y as i32 + top);
        if x < 0 || y < 0 || x >= image.width() as i32 || y >= image.height() as i32 {
            continue;
        }
        let pixel = image.get_pixel_mut(x as u32, y as u32);
        let alpha = u32::from(mask.get_pixel((x - left) as u32, (y - top) as u32)[0]);
        for channel in &mut pixel.0 {
            *channel =
                ((u32::from(*channel) * (255 - alpha) + u32::from(ink) * alpha + 127) / 255) as u8;
        }
    }
}

pub(super) fn caption(image: &mut RgbImage, rect: Rect) {
    for y in rect.y..=rect.y + rect.h {
        for x in rect.x..=rect.x + rect.w {
            let border = x < rect.x + 2
                || x > rect.x + rect.w - 2
                || y < rect.y + 2
                || y > rect.y + rect.h - 2;
            image.put_pixel(x as u32, y as u32, Rgb([if border { 15 } else { 255 }; 3]));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::validation::bytes_hash;

    #[test]
    fn supersampled_balloon_and_tail_match_pillow() {
        let rect = Rect {
            x: 25,
            y: 20,
            w: 111,
            h: 71,
        };
        let mut image = RgbImage::from_pixel(270, 160, Rgb([86, 170, 225]));
        let tail = tail(rect, [0.8, 0.7], [270, 160], "human").unwrap();
        balloon(&mut image, rect, &tail).unwrap();
        assert_eq!(
            bytes_hash(image.as_raw()),
            "7cb3928fac4d2c83792aa38fd2e66c0cc22645799fce327cd78e4efbca0647b2"
        );
    }
}
