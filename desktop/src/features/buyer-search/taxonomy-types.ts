export type BuyerTaxonomyJson = null | boolean | number | string | BuyerTaxonomyJson[] | { [key: string]: BuyerTaxonomyJson }

export interface BuyerTaxonomySnapshot {
  snapshot_id: string
  source: string
  source_revision: string | null
  captured_at: string
  provenance: Record<string, BuyerTaxonomyJson>
  content_hash: string
  category_count: number
  attribute_count: number
  filter_count: number
  term_count: number
  created_at: string
}

export interface BuyerTaxonomyCategory {
  category_id: number
  name: string
  parent_category_id: number | null
  rubric_id: number | null
  classifier_id: number | null
  category_path: number[]
  source_path: string | null
  metadata: Record<string, BuyerTaxonomyJson>
}

export interface BuyerTaxonomyDescriptor {
  category_id: number
  descriptor_id: string
  name: string
  values: BuyerTaxonomyJson[]
  metadata: Record<string, BuyerTaxonomyJson>
}

export interface BuyerTaxonomyTerm {
  kind: string
  category_id: number | null
  text: string
  metadata: Record<string, BuyerTaxonomyJson>
}

export interface BuyerTaxonomyVocabularyItem {
  text: string
  source: string
  category_id: number | null
  metadata: Record<string, BuyerTaxonomyJson>
}

export interface BuyerTaxonomyGenerationDefaults {
  category_id: number
  category_path: number[]
  taxonomy_snapshot_id: string
  taxonomy_source: string
}

export interface BuyerTaxonomyCategoryContext {
  snapshot: BuyerTaxonomySnapshot
  category: BuyerTaxonomyCategory
  ancestors: BuyerTaxonomyCategory[]
  children: BuyerTaxonomyCategory[]
  attributes: BuyerTaxonomyDescriptor[]
  filters: BuyerTaxonomyDescriptor[]
  terms: BuyerTaxonomyTerm[]
  vocabulary: BuyerTaxonomyVocabularyItem[]
  generation_defaults: BuyerTaxonomyGenerationDefaults
}

export interface BuyerTaxonomyCategoryPage {
  snapshot: BuyerTaxonomySnapshot
  items: BuyerTaxonomyCategory[]
  next_after_category_id: number | null
}

export interface BuyerTaxonomySelection {
  snapshot: BuyerTaxonomySnapshot
  category: BuyerTaxonomyCategory
  categoryPath: BuyerTaxonomyCategory[]
  categoryScope: Record<string, BuyerTaxonomyJson>
  /** Undefined while the Market total is loading; null when it is unavailable. */
  kworksCount?: number | null
  generationDefaults: BuyerTaxonomyGenerationDefaults
  context: BuyerTaxonomyCategoryContext
}
