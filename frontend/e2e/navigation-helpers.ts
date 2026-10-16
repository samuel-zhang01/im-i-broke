import { expect, type Page } from '@playwright/test';
import { bankingWorkspace } from '../src/navigation';

export async function navigateFinance(page: Page, id: string, title: string) {
  await page.locator('nav[aria-label="Main navigation"]').waitFor({ state: 'attached' });
  const workspace = bankingWorkspace(id);
  const picker = page.getByRole('combobox', { name: 'Go to page', exact: true });
  if (await picker.isVisible()) await picker.selectOption(workspace?.home ?? id);
  else await page.getByRole('navigation', { name: 'Main navigation', exact: true }).getByRole('button', { name: workspace?.label ?? title, exact: true }).click();
  if (workspace) {
    const target = workspace.pages.find(item => item.id === id)!;
    await page.getByRole('navigation', { name: `${workspace.label} sections`, exact: true }).getByRole('button', { name: target.label, exact: true }).click();
  }
  await expect(page.getByRole('heading', { level: 1, name: title, exact: true })).toBeVisible();
}

/** Open secondary mobile controls only for tests that need to interact with them. */
export async function revealMobileControls(page: Page, label: string) {
  const toggle = page.getByRole('button', { name: label, exact: true });
  if ((page.viewportSize()?.width ?? 1440) >= 640) return;
  await expect(toggle).toBeVisible();
  if (await toggle.getAttribute('aria-expanded') === 'false') await toggle.click();
}
